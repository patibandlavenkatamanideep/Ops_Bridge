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
from typing import Any

from opsbridge.config import Settings, get_settings
from opsbridge.connectors.purchasing import PurchasingClient
from opsbridge.connectors.tasks import SimTaskConnector, TaskConnector
from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.packs import PackRegistry
from opsbridge.persistence.db import Database, DbContext, get_database
from opsbridge.persistence.models import Tenant, WorkflowRun
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
        except LeaseLost as exc:
            log.warning("job %s %s: %s; stopping without writing results", job.kind, job.id, exc)
            return True
        except StepFailed as exc:
            status = jobs.fail(self.db, job, str(exc), retryable=exc.retryable)
            log.warning("job %s %s failed (%s): %s", job.kind, job.id, status, exc)
            if status in ("failed", "dead"):
                self._on_exhausted(job, str(exc))
            return True
        except Exception as exc:  # unexpected: retry with backoff, never crash the loop
            status = jobs.fail(self.db, job, f"{type(exc).__name__}: {exc}", retryable=True)
            log.exception("job %s %s crashed (%s)", job.kind, job.id, status)
            if status == "dead":
                self._on_exhausted(job, f"{type(exc).__name__}: {exc}")
            return True
        if not jobs.complete(self.db, job):
            log.warning("job %s lease lost before completion; result kept in durable state", job.id)
        return True

    def _on_exhausted(self, job: jobs.ClaimedJob, error: str) -> None:
        if job.kind == "dispatch_operation":
            mark_unknown_after_exhaustion(self.db, job, error)
        else:
            _mark_run_failed(self.db, job, error)

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

            return casework_connector(settings, tenant_id, operation)
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
    return worker


def _investigate(deps: InvestigationDeps, job: jobs.ClaimedJob) -> None:
    run_investigation(deps, job.tenant_id, uuid.UUID(job.payload["run_id"]))


def main() -> None:
    configure_logging()
    worker = make_worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop_event.set()
    time.sleep(0)
