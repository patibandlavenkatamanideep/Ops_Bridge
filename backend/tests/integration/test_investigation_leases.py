"""Investigation jobs are fenced and renew their lease while they work; a job whose worker
keeps dying is given up; a resumed run keeps its model-call budget (review finding F05).

Before: the investigation handler never saw its job, so a step outlasting the 30 s lease
let a second worker run the same investigation, a worker that had lost the job still wrote
its step results, an expired job was re-claimed forever, and a resumed run started a fresh
budget.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text

from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import InvestigationVersion, ModelCall, WorkflowRun, WorkflowStep
from opsbridge.workflow import investigation, jobs
from opsbridge.workflow.investigation import _agent_runtime, run_investigation
from opsbridge.workflow.worker import Worker

from .stack import open_case
from .test_api_cases import call
from .test_approvals_execution import _tenant_of

pytestmark = pytest.mark.integration
ORG = "demo-furniture"


def worker_for(st: dict[str, Any], lease_seconds: int, step_limit: int = 300) -> Worker:
    settings = st["settings"].model_copy(
        update={
            "worker_lease_seconds": lease_seconds,
            "investigation_step_limit_seconds": step_limit,
        }
    )
    w = Worker(db=st["worker"].db, settings=settings)
    deps = replace(st["deps"], settings=settings)
    w.register(
        "investigate",
        lambda job: run_investigation(
            deps, job.tenant_id, uuid.UUID(job.payload["run_id"]), w.lease_for(job)
        ),
    )
    return w


def slow_first_reconcile(monkeypatch: pytest.MonkeyPatch, seconds: float) -> list[int]:
    """The first call takes `seconds` and marks its output; later calls are normal."""
    real, calls = investigation._reconcile, []

    def reconcile(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        out = real(*a, **k)
        if len(calls) == 1:
            time.sleep(seconds)
            return {**out, "written_by": "the slow first worker"}
        return out

    monkeypatch.setattr(investigation, "_reconcile", reconcile)
    return calls


def started_run(
    st: dict[str, Any], invoice: str = "INV-9001", mode: str = "rules_only"
) -> uuid.UUID:
    """A queued investigation run (nothing processed yet)."""
    case_id = open_case(st, "furn-investigator", ORG, invoice)
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"/api/v1/tenants/{ORG}/cases/{case_id}/investigations",
        json={"model_mode": mode},
    )
    assert r.status_code in (200, 202), r.text
    return uuid.UUID(r.json()["run_id"])


def step_output(
    worker_db: Database, tid: uuid.UUID, run_id: uuid.UUID, name: str
) -> dict[str, Any]:
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        out = s.execute(
            select(WorkflowStep.output).where(
                WorkflowStep.run_id == run_id, WorkflowStep.step_name == name
            )
        ).scalar_one()
    return dict(out or {})


def versions(worker_db: Database, tid: uuid.UUID, run_id: uuid.UUID) -> int:
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        return int(
            s.execute(
                select(func.count())
                .select_from(InvestigationVersion)
                .where(InvestigationVersion.run_id == run_id)
            ).scalar_one()
        )


def test_a_slow_step_keeps_its_job_and_no_second_worker_starts_the_run(
    stack, worker_db: Database, migrator_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    run_id = started_run(st)
    calls = slow_first_reconcile(monkeypatch, 4.0)
    first, second = worker_for(st, 2), worker_for(st, 2)
    t = threading.Thread(target=first.run_once)
    t.start()
    time.sleep(3.0)  # past the 2 s lease: only renewal keeps the job
    assert second.run_once() is False
    t.join(30)
    tid = _tenant_of(migrator_engine, ORG)
    assert len(calls) == 1 and versions(worker_db, tid, run_id) == 1


def test_a_stuck_step_loses_its_job_and_its_late_result_is_refused(
    stack, worker_db: Database, migrator_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    run_id = started_run(st)
    slow_first_reconcile(monkeypatch, 4.0)
    stuck, other = worker_for(st, 1, step_limit=1), worker_for(st, 1)
    job = jobs.claim(worker_db, stuck.worker_id, 1, ["investigate"])
    assert job is not None
    result: dict[str, str] = {}
    t = threading.Thread(target=lambda: result.setdefault("stuck", stuck._handle(job)))
    t.start()
    time.sleep(2.5)  # renewal stopped after 1 s; the lease has lapsed
    assert other.run_once() is True  # takes the run over and finishes it
    t.join(30)
    tid = _tenant_of(migrator_engine, ORG)
    assert result["stuck"] == "lease_lost"
    assert "written_by" not in step_output(worker_db, tid, run_id, "reconcile_records")
    assert versions(worker_db, tid, run_id) == 1


def test_a_run_whose_worker_keeps_dying_is_given_up(
    stack, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    run_id = started_run(st)
    while True:  # every attempt's worker dies: the lease simply expires
        job = jobs.claim(worker_db, "dying-worker", 30, ["investigate"])
        assert job is not None
        with worker_db.session(DbContext()) as s:
            s.execute(
                text(
                    "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :i"
                ),
                {"i": job.id},
            )
        if job.attempts >= job.max_attempts:
            break
    assert worker_for(st, 30).run_once() is False  # nothing left to claim: it was given up
    tid = _tenant_of(migrator_engine, ORG)
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None and run.status == "failed" and "stopped" in (run.error or "")
    with worker_db.session(DbContext()) as s:
        assert (
            s.execute(text("SELECT status FROM jobs WHERE id = :i"), {"i": job.id}).scalar()
            == "dead"
        )


def test_a_resumed_run_keeps_counting_its_model_calls(
    stack, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    run_id = started_run(st, mode="mock")
    while st["worker"].run_once():
        pass
    tid = _tenant_of(migrator_engine, ORG)
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        made = s.execute(
            select(func.count()).select_from(ModelCall).where(ModelCall.run_id == run_id)
        ).scalar_one()
    assert made >= 1
    agent = _agent_runtime(st["deps"], tid, run_id, {"policy_version_id": None})
    assert agent["budget"].model_calls == made  # not a fresh budget
