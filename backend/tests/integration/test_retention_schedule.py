"""Scheduled retention (closes "retention runs only on request"): one sweep per tenant with
retention configured, per database day, without the worker reading other tenants' settings."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from opsbridge.persistence.db import Database, make_engine
from opsbridge.workflow import worker as worker_mod
from opsbridge.workflow.retention import schedule_due
from opsbridge.workflow.worker import Worker

from .test_api_cases import call
from .test_approvals_execution import drain
from .test_hardening import tenant_id

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"


def test_one_sweep_per_configured_tenant_per_day(
    stack, worker_db: Database, migrator_engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    assert schedule_due(worker_db) == 0  # nobody configured retention yet
    adm = st["actors"]["furn-admin"]
    assert (
        call(
            st["client"],
            adm,
            "PUT",
            f"{T}/settings",
            json={"retention": {"conversation_days": 365}},
        ).status_code
        == 200
    )
    assert schedule_due(worker_db) == 1
    assert schedule_due(worker_db) == 0  # same day: deduplicated, also across concurrent workers
    furn = tenant_id(migrator_engine, "demo-furniture")
    engine = make_engine("worker", database="opsbridge_test")
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM tenant_settings")).scalar_one() == 0
            )  # RLS intact
            assert list(
                conn.execute(text("SELECT * FROM ops.tenants_with_retention()")).scalars()
            ) == [furn]
            [(tid, status)] = conn.execute(
                text("SELECT tenant_id, status FROM jobs WHERE kind = 'retention_sweep'")
            ).all()
        assert (tid, status) == (furn, "queued")
        drain(st)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT status FROM jobs WHERE kind = 'retention_sweep'")
                ).scalar_one()
                == "succeeded"
            )
    finally:
        engine.dispose()


def test_worker_checks_the_schedule_at_most_once_per_interval(
    stack,
    worker_db: Database,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(worker_mod, "schedule_retention", lambda db: calls.append(db) or 0)
    w = Worker(
        db=worker_db,
        settings=stack["settings"].model_copy(update={"retention_schedule_seconds": 3600}),
    )
    w.register("retention_sweep", lambda job: None)
    w._maybe_schedule()
    w._maybe_schedule()
    assert len(calls) == 1
    off = Worker(
        db=worker_db,
        settings=stack["settings"].model_copy(update={"retention_schedule_seconds": 0}),
    )
    off.register("retention_sweep", lambda job: None)
    off._maybe_schedule()
    assert len(calls) == 1  # 0 disables scheduling
