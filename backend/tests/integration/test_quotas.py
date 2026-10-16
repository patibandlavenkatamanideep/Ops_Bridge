"""Per-organization usage quotas (OB-27): tier entitlements bound compute for the whole
tenant; exceeding them is refused (runs) or falls back to rules (model calls), never shared
across organizations and never an authority grant."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from opsbridge.auth import permissions
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import ModelCall

from .stack import get, open_case
from .test_api_cases import call
from .test_approvals_execution import drain
from .test_hardening import tenant_id

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"


def run(st, who: str, tenant: str, case_id: str, mode: str = "rules_only"):  # type: ignore[no-untyped-def]
    return call(
        st["client"],
        st["actors"][who],
        "POST",
        f"/api/v1/tenants/{tenant}/cases/{case_id}/investigations",
        json={"model_mode": mode},
    )


def test_hourly_run_quota_is_per_organization(stack, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    st = stack
    monkeypatch.setitem(
        permissions.TIERS,
        "standard",
        replace(permissions.TIERS["standard"], max_investigations_per_hour=2),
    )
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
    for _ in range(2):
        assert run(st, "furn-investigator", "demo-furniture", case_id).status_code == 202
        drain(st)
    refused = run(st, "furn-investigator", "demo-furniture", case_id)
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "quota_exceeded"
    assert refused.json()["error"]["retryable"] is True
    assert 1 <= int(refused.headers["retry-after"]) <= 3601
    # Another organization (enhanced tier, its own count) is unaffected.
    it_case = open_case(st, "it-investigator", "demo-it", "INV-7002")
    assert run(st, "it-investigator", "demo-it", it_case).status_code == 202


def test_daily_model_call_quota_falls_back_to_rules(
    stack,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
    worker_db: Database,
    migrator_engine,
) -> None:
    st = stack
    monkeypatch.setitem(
        permissions.TIERS,
        "standard",
        replace(permissions.TIERS["standard"], max_model_calls_per_day=1),
    )
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
    assert run(st, "furn-investigator", "demo-furniture", case_id, mode="mock").status_code == 202
    drain(st)
    inv = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert inv["outcome"] == "discrepancies_found"  # the deterministic finding stands
    with worker_db.session(DbContext(tenant_id=tenant_id(migrator_engine, "demo-furniture"))) as s:
        outcomes = [
            o for (o,) in s.execute(select(ModelCall.outcome).order_by(ModelCall.created_at))
        ]
    assert outcomes[0] == "ok" and "quota_exceeded" in outcomes[1:]
    assert sum(1 for o in outcomes if o != "quota_exceeded") == 1  # only one call reached the model
    draft = inv["recommendation"]["proposal_draft"]
    assert draft["origin"] == "rules" and any(
        "quota_exceeded" in p for p in draft["validation"]["problems"]
    )
