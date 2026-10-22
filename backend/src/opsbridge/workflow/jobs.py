"""PostgreSQL job queue with leases and fencing tokens.

- `enqueue` runs inside the caller's business transaction (transactional outbox).
- `claim` uses FOR UPDATE SKIP LOCKED, sets a lease and increments `fencing_token`.
  A job whose lease expired is claimable again while it has attempts left; one that has
  used them all (its worker kept dying) is given up by `reap_exhausted` instead.
- `complete`/`fail` succeed only if the caller still holds the current fencing token,
  so a stale worker (lease lost) cannot overwrite the new holder's outcome.
- `Lease` lets a handler doing long work fence its own durable writes and renew the lease
  while a step makes progress, for a bounded time.
Delivery is at-least-once: handlers must be idempotent with respect to durable state.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Job
from opsbridge.telemetry import tracing


class LeaseLost(Exception):
    """This worker no longer holds the job; it must stop without writing results."""


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    fencing_token: int
    lease_owner: str


def enqueue(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    priority: int = 100,
    max_attempts: int = 5,
    run_after: datetime | None = None,
) -> Job:
    trace_ctx = tracing.carrier()
    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        # W3C trace context lets the worker continue the requester's trace.
        payload={**payload, "_trace": trace_ctx} if trace_ctx else payload,
        status="queued",
        priority=priority,
        attempts=0,
        max_attempts=max_attempts,
        fencing_token=0,
        dedupe_key=dedupe_key,
    )
    if run_after is not None:
        job.run_after = run_after
    session.add(job)
    session.flush()
    return job


def claim(
    db: Database, worker_id: str, lease_seconds: int, kinds: list[str] | None = None
) -> ClaimedJob | None:
    """Claim one runnable job (queued, or leased with an expired lease)."""
    kind_filter = "AND kind = ANY(:kinds)" if kinds else ""
    sql = text(f"""
        UPDATE jobs SET status = 'leased', lease_owner = :worker,
               lease_expires_at = now() + make_interval(secs => :lease),
               fencing_token = fencing_token + 1, attempts = attempts + 1, updated_at = now()
        WHERE id = (
          SELECT id FROM jobs
          WHERE ((status = 'queued' AND run_after <= now())
                 OR (status = 'leased' AND lease_expires_at < now()
                     AND attempts < max_attempts))
                {kind_filter}
          ORDER BY priority, run_after
          FOR UPDATE SKIP LOCKED
          LIMIT 1)
        RETURNING id, tenant_id, kind, payload, attempts, max_attempts, fencing_token, lease_owner
    """)  # noqa: S608 - kind_filter is a fixed fragment
    with db.session(DbContext()) as s:
        row = s.execute(sql, {"worker": worker_id, "lease": lease_seconds, "kinds": kinds}).first()
        if row is None:
            return None
        return ClaimedJob(
            row.id,
            row.tenant_id,
            row.kind,
            dict(row.payload),
            row.attempts,
            row.max_attempts,
            row.fencing_token,
            row.lease_owner,
        )


def reap_exhausted(db: Database, kinds: list[str] | None = None) -> list[ClaimedJob]:
    """Give up jobs whose lease expired on their last attempt: the worker died every time
    (a crash, an out-of-memory kill), so no failure was recorded and `claim` would otherwise
    hand them out forever. Returns them for the caller's exhaustion handling."""
    kind_filter = "AND kind = ANY(:kinds)" if kinds else ""
    sql = text(f"""
        UPDATE jobs SET status = 'dead', lease_owner = NULL, lease_expires_at = NULL,
               last_error = 'lease expired on the last attempt; the worker stopped each time',
               updated_at = now()
        WHERE id IN (
          SELECT id FROM jobs
          WHERE status = 'leased' AND lease_expires_at < now() AND attempts >= max_attempts
                {kind_filter}
          FOR UPDATE SKIP LOCKED
          LIMIT 20)
        RETURNING id, tenant_id, kind, payload, attempts, max_attempts, fencing_token,
                  'reaper' AS lease_owner
    """)  # noqa: S608 - kind_filter is a fixed fragment
    with db.session(DbContext()) as s:
        rows = s.execute(sql, {"kinds": kinds}).all()
    return [
        ClaimedJob(
            r.id,
            r.tenant_id,
            r.kind,
            dict(r.payload),
            r.attempts,
            r.max_attempts,
            r.fencing_token,
            r.lease_owner,
        )
        for r in rows
    ]


def heartbeat(db: Database, job: ClaimedJob, lease_seconds: int) -> bool:
    with db.session(DbContext()) as s:
        n = _rows(
            s.execute(
                text(
                    "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => :lease), updated_at = now() "
                    "WHERE id = :id AND fencing_token = :tok AND status = 'leased'"
                ),
                {"lease": lease_seconds, "id": job.id, "tok": job.fencing_token},
            )
        )
        return bool(n)


def holds_lease(session: Session, job: ClaimedJob) -> bool:
    """Check (inside a business transaction) that this worker still owns the job."""
    row = session.execute(
        text(
            "SELECT 1 FROM jobs WHERE id = :id AND fencing_token = :tok AND status = 'leased' FOR UPDATE"
        ),
        {"id": job.id, "tok": job.fencing_token},
    ).first()
    return row is not None


@dataclass
class Lease:
    """The lease on a claimed job, for handlers doing long work. `check` (inside a business
    transaction) fences durable writes; `keep_alive` renews the lease while a step runs,
    for at most `max_seconds`: a step stuck longer lets the lease expire, so another worker
    can take the job and this one's late writes are refused."""

    db: Database
    job: ClaimedJob
    lease_seconds: int
    on_renew: Callable[[], None] | None = None  # e.g. the worker's liveness clock

    def check(self, session: Session) -> None:
        if not holds_lease(session, self.job):
            raise LeaseLost(f"job {self.job.id} lease lost (token {self.job.fencing_token})")

    @contextmanager
    def keep_alive(self, max_seconds: float) -> Iterator[None]:
        stop = threading.Event()

        def renew() -> None:
            deadline = time.monotonic() + max_seconds
            while not stop.wait(self.lease_seconds / 3):
                if time.monotonic() > deadline:
                    return  # stuck: stop renewing and stop looking alive
                if not heartbeat(self.db, self.job, self.lease_seconds):
                    return  # lost already
                if self.on_renew is not None:
                    self.on_renew()

        thread = threading.Thread(target=renew, name=f"lease-{self.job.id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()


def complete(db: Database, job: ClaimedJob) -> bool:
    with db.session(DbContext()) as s:
        n = _rows(
            s.execute(
                text(
                    "UPDATE jobs SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL, updated_at = now() "
                    "WHERE id = :id AND fencing_token = :tok AND status = 'leased'"
                ),
                {"id": job.id, "tok": job.fencing_token},
            )
        )
        return bool(n)


def fail(
    db: Database, job: ClaimedJob, error: str, *, retryable: bool, base_delay: float = 2.0
) -> str:
    """Record a failure. Retryable failures are re-queued with jittered exponential backoff."""
    exhausted = job.attempts >= job.max_attempts
    status = "queued" if retryable and not exhausted else ("dead" if retryable else "failed")
    delay = min(300.0, base_delay * (2 ** (job.attempts - 1))) * (0.5 + random.random())  # noqa: S311
    with db.session(DbContext()) as s:
        n = _rows(
            s.execute(
                text(
                    "UPDATE jobs SET status = :status, last_error = :err, lease_owner = NULL, lease_expires_at = NULL, "
                    "run_after = now() + make_interval(secs => :delay), updated_at = now() "
                    "WHERE id = :id AND fencing_token = :tok AND status = 'leased'"
                ),
                {
                    "status": status,
                    "err": error[:2000],
                    "delay": delay,
                    "id": job.id,
                    "tok": job.fencing_token,
                },
            )
        )
    return status if n else "lease_lost"


def utcnow() -> datetime:
    return datetime.now(UTC)


def later(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


def _rows(result: Any) -> int:
    """Rows affected by an UPDATE (CursorResult.rowcount)."""
    return int(cast(CursorResult[Any], result).rowcount)
