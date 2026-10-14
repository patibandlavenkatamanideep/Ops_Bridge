"""Background worker: claims leased jobs and runs their handlers under the job's tenant."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from opsbridge.config import Settings, get_settings
from opsbridge.connectors.purchasing import PurchasingClient
from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.packs import PackRegistry
from opsbridge.persistence.db import Database, DbContext, get_database
from opsbridge.persistence.models import WorkflowRun

from . import jobs
from .investigation import InvestigationDeps, StepFailed, run_investigation

log = logging.getLogger("opsbridge.worker")

Handler = Callable[[jobs.ClaimedJob], None]


def build_investigation_deps(settings: Settings, db: Database) -> InvestigationDeps:
    packs = PackRegistry(settings.packs_dir)
    secret = settings.sim_master_secret.get_secret_value()

    def purchasing_for(org: str) -> PurchasingClient:
        return PurchasingClient(
            settings.simulator_url, secret, org, timeout=settings.connector_timeout_seconds
        )

    return InvestigationDeps(
        db=db,
        store=LocalArtifactStore(settings.artifact_dir),
        pack_for=packs.get,
        purchasing_for=purchasing_for,
    )


@dataclass
class Worker:
    db: Database
    settings: Settings
    worker_id: str = field(
        default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    )
    handlers: dict[str, Handler] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def run_once(self) -> bool:
        """Claim and process at most one job. Returns True if a job was processed."""
        job = jobs.claim(
            self.db, self.worker_id, self.settings.worker_lease_seconds, list(self.handlers)
        )
        if job is None:
            return False
        handler = self.handlers[job.kind]
        try:
            handler(job)
        except StepFailed as exc:
            status = jobs.fail(self.db, job, str(exc), retryable=exc.retryable)
            log.warning("job %s %s failed (%s): %s", job.kind, job.id, status, exc)
            if status in ("failed", "dead"):
                _mark_run_failed(self.db, job, str(exc))
            return True
        except Exception as exc:  # unexpected: retry with backoff, never crash the loop
            status = jobs.fail(self.db, job, f"{type(exc).__name__}: {exc}", retryable=True)
            log.exception("job %s %s crashed (%s)", job.kind, job.id, status)
            if status == "dead":
                _mark_run_failed(self.db, job, f"{type(exc).__name__}: {exc}")
            return True
        if not jobs.complete(self.db, job):
            log.warning("job %s lease lost before completion; result kept in durable state", job.id)
        return True

    def run_forever(self) -> None:
        log.info("worker %s started; handlers=%s", self.worker_id, sorted(self.handlers))
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(self.settings.worker_poll_seconds)


def _mark_run_failed(db: Database, job: jobs.ClaimedJob, error: str) -> None:
    run_id = job.payload.get("run_id")
    if not run_id:
        return
    with db.session(DbContext(tenant_id=job.tenant_id)) as s:
        run = s.get(WorkflowRun, uuid.UUID(run_id))
        if run is not None and run.status != "completed":
            run.status = "failed"
            run.error = error[:2000]


def make_worker(settings: Settings | None = None, db: Database | None = None) -> Worker:
    settings = settings or get_settings()
    db = db or get_database("worker")
    worker = Worker(db=db, settings=settings)
    deps = build_investigation_deps(settings, db)
    worker.register("investigate", lambda job: _investigate(deps, job))
    return worker


def _investigate(deps: InvestigationDeps, job: jobs.ClaimedJob) -> None:
    run_investigation(deps, job.tenant_id, uuid.UUID(job.payload["run_id"]))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = make_worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop_event.set()
    time.sleep(0)
