"""Prometheus metrics (prometheus_client). Labels are bounded: never tenant, case, user,
operation or run identifiers — those belong in traces. Counters that describe database
state changes are incremented after the transaction commits.

Scrape-time collectors read queue and business state through the database: the queue from
`jobs` (the worker role may see all jobs) and tenant-scoped backlogs only through the
aggregate-only function `ops.metrics_snapshot()` (migration 0004), which returns counts and
ages, never rows or tenant identifiers.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

__all__ = ["CONTENT_TYPE_LATEST", "render"]

_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
_WAIT_BUCKETS = (10, 60, 300, 900, 1800, 3600, 4 * 3600, 8 * 3600, 24 * 3600, 72 * 3600)

HTTP_REQUESTS = Counter(
    "opsbridge_http_requests",
    "HTTP requests by route template",
    ["method", "route", "status_class"],
)
HTTP_DURATION = Histogram(
    "opsbridge_http_request_duration_seconds",
    "HTTP request duration",
    ["route"],
    buckets=_DURATION_BUCKETS,
)
JOBS = Counter("opsbridge_jobs_processed", "Jobs processed by the worker", ["kind", "result"])
JOB_DURATION = Histogram(
    "opsbridge_job_duration_seconds", "Job handler duration", ["kind"], buckets=_DURATION_BUCKETS
)
INVESTIGATION_DURATION = Histogram(
    "opsbridge_investigation_duration_seconds",
    "Run requested to investigation recorded (queue wait included)",
    ["mode", "outcome"],
    buckets=_DURATION_BUCKETS,
)
APPROVAL_WAIT = Histogram(
    "opsbridge_approval_wait_seconds",
    "Proposal submitted for approval to decision (human wait)",
    ["decision"],
    buckets=_WAIT_BUCKETS,
)
EXECUTION_DURATION = Histogram(
    "opsbridge_execution_duration_seconds",
    "Approval consumed to terminal operation state",
    ["status"],
    buckets=_DURATION_BUCKETS,
)
OPERATIONS_FINISHED = Counter(
    "opsbridge_operations_finished", "Operations reaching a terminal state", ["status", "connector"]
)
CONNECTOR_ATTEMPTS = Counter(
    "opsbridge_connector_attempts", "External connector attempts", ["phase", "result"]
)
CONNECTOR_DURATION = Histogram(
    "opsbridge_connector_attempt_duration_seconds",
    "External connector attempt duration",
    ["phase"],
    buckets=_DURATION_BUCKETS,
)
MODEL_CALLS = Counter("opsbridge_model_calls", "Model calls", ["provider", "step", "outcome"])
MODEL_DURATION = Histogram(
    "opsbridge_model_call_duration_seconds",
    "Model call duration",
    ["provider"],
    buckets=_DURATION_BUCKETS,
)
MODEL_TOKENS = Counter(
    "opsbridge_model_tokens", "Model tokens reported by providers", ["provider", "direction"]
)
MODEL_COST = Counter(
    "opsbridge_model_cost_estimated_usd",
    "Estimated model cost (hosted providers, before the call)",
    ["provider"],
)
TOOL_CALLS = Counter("opsbridge_tool_calls", "Agent tool calls", ["tool", "outcome", "transport"])
TOOL_DURATION = Histogram(
    "opsbridge_tool_call_duration_seconds",
    "Agent tool call duration",
    ["tool"],
    buckets=_DURATION_BUCKETS,
)


def after_commit(session: Session, fn: Callable[[], None]) -> None:
    """Run `fn` once, only if the session's transaction commits."""
    event.listen(session, "after_commit", lambda _s: fn(), once=True)


class _PoolCollector(Collector):
    def __init__(self) -> None:
        self._pools: dict[str, Engine] = {}
        self._lock = threading.Lock()

    def register(self, name: str, engine: Engine) -> None:
        with self._lock:
            self._pools[name] = engine

    def collect(self) -> Iterator[GaugeMetricFamily]:
        out = GaugeMetricFamily(
            "opsbridge_db_pool_connections", "Pooled connections", labels=["pool", "state"]
        )
        with self._lock:
            pools = list(self._pools.items())
        for name, engine in pools:
            pool: Any = engine.pool
            try:
                out.add_metric([name, "checked_out"], float(pool.checkedout()))
                out.add_metric([name, "capacity"], float(pool.size() + max(pool._max_overflow, 0)))
            except AttributeError:  # non-queue pools (tests) report nothing
                continue
        yield out


POOLS = _PoolCollector()
REGISTRY.register(POOLS)


class _Storage:
    """Artifact store size (cached; a full walk is not run on every scrape) and free space."""

    def __init__(self, root: Path, ttl_seconds: float = 60.0) -> None:
        self.root = root
        self.ttl = ttl_seconds
        self._cached: tuple[float, float] = (-1e9, 0.0)

    def _size(self) -> float:
        at, size = self._cached
        if time.monotonic() - at < self.ttl:
            return size
        total = 0.0
        if self.root.exists():
            for p in self.root.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        self._cached = (time.monotonic(), total)
        return total

    def collect(self) -> Iterator[GaugeMetricFamily]:
        size = GaugeMetricFamily(
            "opsbridge_artifact_store_bytes", "Bytes held in the artifact store"
        )
        size.add_metric([], self._size())
        yield size
        fs = GaugeMetricFamily(
            "opsbridge_artifact_fs_bytes", "Filesystem holding artifacts", labels=["state"]
        )
        target = self.root if self.root.exists() else self.root.parent
        usage = shutil.disk_usage(target)
        fs.add_metric(["free"], float(usage.free))
        fs.add_metric(["total"], float(usage.total))
        yield fs


class _State:
    """Queue and business backlog gauges, read at scrape time (worker process)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def collect(self) -> Iterator[GaugeMetricFamily]:
        up = GaugeMetricFamily("opsbridge_state_scrape_ok", "1 if the state query succeeded")
        depth = GaugeMetricFamily(
            "opsbridge_queue_jobs", "Jobs by kind and status", labels=["kind", "status"]
        )
        age = GaugeMetricFamily(
            "opsbridge_queue_oldest_age_seconds", "Age of the oldest runnable job", labels=["kind"]
        )
        expired = GaugeMetricFamily(
            "opsbridge_queue_expired_leases", "Leased jobs whose lease has expired"
        )
        snap = GaugeMetricFamily("opsbridge_state", "Aggregate business state", labels=["metric"])
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SET LOCAL statement_timeout = 2000"))
                for kind, status, n in conn.execute(
                    text("SELECT kind, status, count(*) FROM jobs GROUP BY kind, status")
                ):
                    depth.add_metric([kind, status], float(n))
                for kind, oldest in conn.execute(
                    text(
                        "SELECT kind, extract(epoch FROM now() - min(run_after)) FROM jobs "
                        "WHERE status = 'queued' AND run_after <= now() GROUP BY kind"
                    )
                ):
                    age.add_metric([kind], float(oldest or 0))
                n_expired = conn.execute(
                    text(
                        "SELECT count(*) FROM jobs WHERE status = 'leased' AND lease_expires_at < now()"
                    )
                ).scalar_one()
                expired.add_metric([], float(n_expired))
                for name, value in conn.execute(
                    text("SELECT metric, value FROM ops.metrics_snapshot()")
                ):
                    snap.add_metric([name], float(value or 0))
            up.add_metric([], 1.0)
        except Exception:  # a scrape must not crash; the gauge says the state is unknown
            up.add_metric([], 0.0)
        yield up
        yield depth
        yield age
        yield expired
        yield snap


class _Dependencies:
    """Readiness of dependencies as gauges (cached so scrapes stay cheap)."""

    def __init__(self, checks: dict[str, Callable[[], bool]], ttl_seconds: float = 15.0) -> None:
        self.checks = checks
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, bool]] = {}

    def status(self) -> dict[str, bool]:
        now = time.monotonic()
        out = {}
        for name, check in self.checks.items():
            at, ok = self._cache.get(name, (-1e9, False))
            if now - at >= self.ttl:
                try:
                    ok = bool(check())
                except Exception:
                    ok = False
                self._cache[name] = (now, ok)
            out[name] = ok
        return out

    def collect(self) -> Iterator[GaugeMetricFamily]:
        g = GaugeMetricFamily(
            "opsbridge_dependency_up", "1 if the dependency is usable", labels=["dependency"]
        )
        for name, ok in self.status().items():
            g.add_metric([name], 1.0 if ok else 0.0)
        yield g


class _Process(Collector):
    """One registered collector whose sources each process sets (and tests may replace)."""

    def __init__(self) -> None:
        self.storage: _Storage | None = None
        self.state: _State | None = None
        self.dependencies: _Dependencies | None = None

    def describe(self) -> list[Any]:
        return []  # do not run the collectors (and their queries) at registration

    def collect(self) -> Iterator[GaugeMetricFamily]:
        for source in (self.storage, self.state, self.dependencies):
            if source is not None:
                yield from source.collect()


PROCESS = _Process()
REGISTRY.register(PROCESS)


def watch_storage(root: Path) -> None:
    PROCESS.storage = _Storage(root)


def watch_state(engine: Engine) -> None:
    PROCESS.state = _State(engine)


def watch_dependencies(checks: dict[str, Callable[[], bool]]) -> _Dependencies:
    PROCESS.dependencies = _Dependencies(checks)
    return PROCESS.dependencies


def render() -> bytes:
    return generate_latest(REGISTRY)


def status_class(code: int) -> str:
    return f"{code // 100}xx"
