"""Background worker: claims leased jobs and runs their handlers under the job's tenant."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from opsbridge.config import Settings, get_settings
from opsbridge.connectors.purchasing import PurchasingClient
from opsbridge.connectors.tasks import SimTaskConnector, TaskConnector
from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.packs import PackRegistry
from opsbridge.persistence.db import Database, DbContext, get_database
from opsbridge.persistence.models import Tenant, WorkflowRun
from opsbridge.telemetry import metrics, tracing
from opsbridge.telemetry.logging import configure_logging

from . import jobs
from .execution import ExecutionDeps, LeaseLost, dispatch, mark_unknown_after_exhaustion
from .investigation import InvestigationDeps, StepFailed, run_investigation
from .retention import sweep as retention_sweep

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
        settings=settings,
        records_mcp=_records_client(settings),
        signer=_signer(settings),
    )


def _records_client(settings: Settings) -> Any:
    if settings.tool_transport != "mcp":
        return None
    from opsbridge.mcp_services.factory import records_client

    return records_client(settings)


def _signer(settings: Settings) -> Any:
    if settings.tool_transport != "mcp":
        return None
    from opsbridge.mcp_services.factory import signer

    return signer(settings)


@dataclass
class Worker:
    db: Database
    settings: Settings
    worker_id: str = field(
        default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    )
    handlers: dict[str, Handler] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    last_loop: float = field(default_factory=time.monotonic)

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def run_once(self) -> bool:
        """Claim and process at most one job. Returns True if a job was processed."""
        self.last_loop = time.monotonic()
        job = jobs.claim(
            self.db, self.worker_id, self.settings.worker_lease_seconds, list(self.handlers)
        )
        if job is None:
            return False
        started = time.monotonic()
        with tracing.span(
            f"job {job.kind}",
            kind=SpanKind.CONSUMER,
            context=tracing.context_from(job.payload.get("_trace")),
            attributes={"opsbridge.job.attempt": job.attempts},
            tenant_id=job.tenant_id,
            job_id=job.id,
            run_id=job.payload.get("run_id"),
            operation_id=job.payload.get("operation_id"),
        ) as sp:
            result = self._handle(job)
            sp.set_attribute("opsbridge.job.result", result)
        metrics.JOBS.labels(job.kind, result).inc()
        metrics.JOB_DURATION.labels(job.kind).observe(time.monotonic() - started)
        return True

    def _handle(self, job: jobs.ClaimedJob) -> str:
        handler = self.handlers[job.kind]
        try:
            handler(job)
        except LeaseLost as exc:
            log.warning("job %s %s: %s; stopping without writing results", job.kind, job.id, exc)
            return "lease_lost"
        except StepFailed as exc:
            status = jobs.fail(self.db, job, str(exc), retryable=exc.retryable)
            log.warning("job %s %s failed (%s): %s", job.kind, job.id, status, exc)
            if status in ("failed", "dead"):
                self._on_exhausted(job, str(exc))
            return "retry" if status == "queued" else status
        except Exception as exc:  # unexpected: retry with backoff, never crash the loop
            status = jobs.fail(self.db, job, f"{type(exc).__name__}: {exc}", retryable=True)
            log.exception("job %s %s crashed (%s)", job.kind, job.id, status)
            if status == "dead":
                self._on_exhausted(job, f"{type(exc).__name__}: {exc}")
            return "crash_retry" if status == "queued" else f"crash_{status}"
        if not jobs.complete(self.db, job):
            log.warning("job %s lease lost before completion; result kept in durable state", job.id)
            return "lease_lost"
        return "completed"

    def _on_exhausted(self, job: jobs.ClaimedJob, error: str) -> None:
        if job.kind == "dispatch_operation":
            mark_unknown_after_exhaustion(self.db, job, error)
        else:
            _mark_run_failed(self.db, job, error)

    def run_forever(self) -> None:
        log.info("worker %s started; handlers=%s", self.worker_id, sorted(self.handlers))
        backoff = 0.0
        while not self.stop_event.is_set():
            try:
                processed = self.run_once()
            except DBAPIError as exc:
                # Database unavailable (restart, failover, network): keep the process and back
                # off. An in-flight job's lease expires and its fencing token stops this worker
                # from writing late results, so another claim resumes it from durable steps.
                backoff = min(max(backoff * 2, 0.5), 30.0)
                log.warning(
                    "database unavailable (%s); retrying in %.1fs",
                    type(exc.orig or exc).__name__,
                    backoff,
                )
                self.stop_event.wait(backoff)
                continue
            backoff = 0.0
            self.last_loop = time.monotonic()
            if not processed:
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


def build_execution_deps(settings: Settings, db: Database) -> ExecutionDeps:
    if settings.worker_lease_seconds < 2 * settings.connector_timeout_seconds:
        raise ValueError(
            "worker_lease_seconds must be at least twice connector_timeout_seconds so a slow "
            "worker is not mistaken for a crashed one"
        )
    secret = settings.sim_master_secret.get_secret_value()

    def connector_for(tenant_id: uuid.UUID, name: str, operation: dict[str, Any]) -> TaskConnector:
        with db.session(DbContext(tenant_id=tenant_id)) as s:
            tenant = s.get(Tenant, tenant_id)
            assert tenant is not None
            org = tenant.slug
        if name == "sim_tasks" and settings.tool_transport == "mcp":
            from opsbridge.mcp_services.factory import casework_connector

            return casework_connector(settings, tenant_id, operation, org)
        if name == "sim_tasks":
            return SimTaskConnector(settings.simulator_url, secret, org)
        if name == "github_issues":
            from opsbridge.connectors.github import github_connector_from_settings

            return github_connector_from_settings(settings)
        raise ValueError(f"unknown task connector {name!r}")

    return ExecutionDeps(
        db=db,
        connector_for=connector_for,
        timeout_seconds=settings.connector_timeout_seconds,
        lease_seconds=settings.worker_lease_seconds,
    )


def make_worker(settings: Settings | None = None, db: Database | None = None) -> Worker:
    settings = settings or get_settings()
    db = db or get_database("worker")
    worker = Worker(db=db, settings=settings)
    deps = build_investigation_deps(settings, db)
    worker.register("investigate", lambda job: _investigate(deps, job))
    exec_deps = build_execution_deps(settings, db)
    worker.register("dispatch_operation", lambda job: dispatch(exec_deps, job))
    store = LocalArtifactStore(settings.artifact_dir)

    def _retention(job: jobs.ClaimedJob) -> None:
        report = retention_sweep(db, store, job.tenant_id)
        log.info("retention sweep for tenant %s: %s", job.tenant_id, report.to_dict())

    worker.register("retention_sweep", _retention)
    metrics.POOLS.register("worker", db.engine)
    metrics.watch_state(db.engine)
    metrics.watch_storage(settings.artifact_dir)
    return worker


class _HealthHandler(BaseHTTPRequestHandler):
    worker: Worker

    def log_message(self, format: str, *args: Any) -> None:
        return None  # scrapes and probes are not logged

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        w = self.worker
        stale_after = max(60.0, 2.0 * w.settings.worker_lease_seconds)
        loop_age = time.monotonic() - w.last_loop
        if self.path == "/healthz":
            ok = loop_age < stale_after
            self._send(
                200 if ok else 503,
                json.dumps({"live": ok, "loop_age_seconds": round(loop_age, 1)}).encode(),
            )
        elif self.path == "/readyz":
            checks = {"loop": "ok" if loop_age < stale_after else "stale"}
            try:
                with w.db.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                checks["database"] = "ok"
            except Exception as exc:
                checks["database"] = f"error:{type(exc).__name__}"
            ready = all(v == "ok" for v in checks.values())
            self._send(
                200 if ready else 503, json.dumps({"ready": ready, "checks": checks}).encode()
            )
        elif self.path == "/metrics":
            self._send(200, metrics.render(), metrics.CONTENT_TYPE_LATEST)
        else:
            self._send(404, b"{}")


def serve_health(worker: Worker, host: str, port: int) -> ThreadingHTTPServer:
    """Liveness (loop heartbeat), readiness (database + loop) and metrics for the worker."""
    handler = type("Handler", (_HealthHandler,), {"worker": worker})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, name="worker-health", daemon=True).start()
    return server


def _investigate(deps: InvestigationDeps, job: jobs.ClaimedJob) -> None:
    run_investigation(deps, job.tenant_id, uuid.UUID(job.payload["run_id"]))


def main() -> None:
    configure_logging()
    tracing.configure_tracing("opsbridge-worker")
    worker = make_worker()
    if worker.settings.worker_http_port:
        serve_health(worker, worker.settings.worker_http_host, worker.settings.worker_http_port)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop_event.set()
    time.sleep(0)
