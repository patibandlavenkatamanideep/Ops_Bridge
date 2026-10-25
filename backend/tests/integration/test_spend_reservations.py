"""Spend and quotas are reserved before the call, not checked and hoped for (finding F06).

Before: the runtime summed recorded spend, compared it with the ceiling and then called the
model, so concurrent workers each saw room and all called; the daily model-call quota and the
hourly investigation quota were count-then-act the same way; a failed or timed-out call was
recorded with no cost although the provider may have charged for it; and nothing at all was
recorded if the worker died during the call.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from opsbridge.agents import runtime
from opsbridge.agents.contracts import Capabilities, ModelRequest, ModelResponse, ModelTimeout
from opsbridge.agents.runtime import Recorder, RunBudget, call_model
from opsbridge.auth import permissions
from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import ModelCall, WorkflowRun
from opsbridge.services.cases import CaseCreate, create_case
from opsbridge.services.investigations import QuotaExceeded, start_investigation

from .stack import open_case
from .test_approvals_execution import _tenant_of

pytestmark = pytest.mark.integration
ORG = "demo-furniture"
REQUEST = ModelRequest("t/1", "sha256:" + "0" * 64, "system", "user text " * 40, {}, 200, 5.0)


class SimulatedCrash(BaseException):
    pass


class FakeHosted:
    """A hosted model stand-in, slow enough that concurrent callers overlap."""

    name = "fake-hosted"
    model = "fake-model"
    capabilities = Capabilities("fake", "none", frozenset(), True, False, True, True, "test double")

    def __init__(self, delay: float = 0.4, fail: BaseException | None = None) -> None:
        self.delay, self.fail, self.calls = delay, fail, 0
        self._lock = threading.Lock()

    def generate(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return ModelResponse(
            "{}", "fake", "fake-model", "fake-model", "live", 100, 50, 400, "end", {}, "fake", None
        )


def priced(st: dict[str, Any], **update: Any) -> Any:
    return st["settings"].model_copy(
        update={
            "anthropic_price_input_per_mtok_usd": "3",
            "anthropic_price_output_per_mtok_usd": "15",
            **update,
        }
    )


def a_run(st: dict[str, Any], worker_db: Database, tid: uuid.UUID) -> uuid.UUID:
    case_id = uuid.UUID(open_case(st, "furn-investigator", ORG, "INV-9001"))
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        run = WorkflowRun(
            tenant_id=tid, case_id=case_id, kind="investigation", status="running",
            workflow_version="t", model_mode="mock", response_mode="live",
            requested_by=st["actors"]["furn-investigator"].user_id, budget={}, usage={},
        )  # fmt: skip
        s.add(run)
        s.flush()
        return run.id


def concurrently(n: int, fn: Any) -> list[Any]:
    barrier, out = threading.Barrier(n), []

    def go() -> None:
        barrier.wait()
        out.append(fn())

    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return out


def one_call(
    st: dict[str, Any], db: Database, tid: uuid.UUID, run: uuid.UUID, adapter: Any, settings: Any,
    quota: int | None = None,
) -> list[str]:  # fmt: skip
    recorder = Recorder(db, LocalArtifactStore(st["settings"].artifact_dir), tid, run)
    budget = RunBudget(100, 0, 200, 5.0, tenant_model_calls_per_day=quota)
    _, problems, _ = call_model(
        adapter, REQUEST, step="explain_findings", recorder=recorder, budget=budget,
        settings=settings, validate=lambda raw: ({}, []),
    )  # fmt: skip
    return problems


def charged(db: Database, tid: uuid.UUID) -> list[tuple[str, Decimal | None]]:
    with db.session(DbContext(tenant_id=tid)) as s:
        return [
            (r.outcome, r.cost_estimate_usd)
            for r in s.execute(select(ModelCall).order_by(ModelCall.created_at)).scalars()
        ]


def test_concurrent_hosted_calls_cannot_overspend_the_ceiling(
    stack, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    tid = _tenant_of(migrator_engine, ORG)
    run = a_run(st, worker_db, tid)
    base = priced(st)
    estimate = runtime._estimate_cost(REQUEST, base)
    settings = priced(st, live_spend_ceiling_usd=str(estimate * 3 + Decimal("0.000001")))
    adapter = FakeHosted()
    results = concurrently(10, lambda: one_call(st, worker_db, tid, run, adapter, settings))
    assert adapter.calls == 3  # funds for three, so three calls reached the provider
    assert sum(1 for p in results if p and p[0].startswith("budget_exceeded")) == 7


def test_concurrent_calls_cannot_exceed_the_daily_quota(
    stack, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    tid = _tenant_of(migrator_engine, ORG)
    run = a_run(st, worker_db, tid)
    adapter = FakeHosted()
    settings = priced(st, live_spend_ceiling_usd="1000")
    concurrently(10, lambda: one_call(st, worker_db, tid, run, adapter, settings, quota=3))
    assert adapter.calls == 3


def test_a_call_whose_worker_died_or_timed_out_stays_charged(
    stack, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    tid = _tenant_of(migrator_engine, ORG)
    run = a_run(st, worker_db, tid)
    estimate = runtime._estimate_cost(REQUEST, priced(st))
    settings = priced(st, live_spend_ceiling_usd=str(estimate * 2 + Decimal("0.000001")))
    with pytest.raises(SimulatedCrash):
        one_call(st, worker_db, tid, run, FakeHosted(0, SimulatedCrash("worker died")), settings)
    timed_out = one_call(
        st, worker_db, tid, run, FakeHosted(0, ModelTimeout("no answer")), settings
    )
    assert timed_out and timed_out[0].startswith("timeout")
    assert [cost for _, cost in charged(worker_db, tid)] == [estimate, estimate]
    # Both may have been charged by the provider, so the ceiling (two calls) is used up.
    refused = one_call(st, worker_db, tid, run, FakeHosted(0), settings)
    assert refused and refused[0].startswith("budget_exceeded")


def test_concurrent_starts_cannot_exceed_the_hourly_investigation_quota(
    stack, app_db: Database, worker_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    monkeypatch.setitem(
        permissions.TIERS,
        "standard",
        dataclasses.replace(permissions.TIERS["standard"], max_investigations_per_hour=3),
    )
    who = st["actors"]["furn-investigator"]
    from opsbridge.api.deps import resolve_principal

    info = SimpleNamespace(user_id=who.user_id, username="furn-investigator", display_name="Ivy")
    principal = resolve_principal(app_db, info, ORG)  # type: ignore[arg-type]
    assert principal is not None
    cases = [
        create_case(app_db, principal, CaseCreate(f"Quota {i}", "", None, None), None)[0].id
        for i in range(10)
    ]
    ids = iter(cases)
    lock = threading.Lock()

    def start() -> str:
        with lock:
            case_id = next(ids)
        try:
            start_investigation(app_db, principal, case_id, "rules_only", None)
            return "started"
        except QuotaExceeded:
            return "refused"

    results = concurrently(10, start)
    assert results.count("started") == 3, results
    with worker_db.session(DbContext(tenant_id=principal.tenant_id)) as s:
        assert s.execute(select(func.count()).select_from(WorkflowRun)).scalar_one() == 3
