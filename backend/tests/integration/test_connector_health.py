"""Tenant settings show policy versions and the follow-up connector's health: what it promises
now (capabilities decide whether an ambiguous create may be resent) and how attempts went."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from opsbridge.api.app import create_app

from .stack import get
from .test_approvals_execution import ORG, approve, drain, operations, prepared

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"


def test_settings_list_policy_versions_with_the_one_in_force(stack: dict[str, Any]) -> None:
    policies = get(stack, "furn-admin", f"{T}/settings").json()["policies"]
    assert all(p["sha256"].startswith("sha256:") for p in policies)
    assert [(p["version"], p["state"]) for p in policies if p["policy_id"] == "furniture-ap"] == [
        (1, "superseded"),
        (2, "active"),
    ]


def test_health_shows_live_capabilities_and_recent_attempts(stack: dict[str, Any]) -> None:
    st = stack
    health = get(st, "furn-admin", f"{T}/connectors/health").json()
    assert health["connector"] == "sim_tasks" and health["reachable"] is True
    assert health["capabilities"] == {"idempotency_keys": True, "lookup": "strong"}
    assert "advertised" in health["capability_source"]
    assert health["attempts_24h"] == [] and health["awaiting_review"] == 0

    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    assert operations(st, case_id)[0]["status"] == "confirmed"
    health = get(st, "furn-auditor", f"{T}/connectors/health").json()  # auditors may read it too
    assert {(a["kind"], a["status"], a["count"]) for a in health["attempts_24h"]} == {
        ("dispatch", "succeeded", 1),
        ("readback", "succeeded", 1),
    }

    st["store"].set_capabilities(ORG, False, False)  # the tracker stops promising safety
    try:
        health = get(st, "furn-admin", f"{T}/connectors/health").json()
    finally:
        st["store"].set_capabilities(ORG, True, True)
    assert health["capabilities"] == {"idempotency_keys": False, "lookup": "none"}
    assert "human review" in health["meaning"]


def test_an_unreachable_tracker_is_reported_as_such(stack: dict[str, Any]) -> None:
    st = stack
    down = st["settings"].model_copy(update={"simulator_url": "http://127.0.0.1:9"})
    with TestClient(
        create_app(settings=down, db=st["client"].app.state.opsbridge.db),
        base_url="http://localhost:5173",
    ) as client:
        client.cookies.set("opsbridge_session", st["actors"]["furn-admin"].token)
        health = client.get(f"{T}/connectors/health").json()
    assert health["reachable"] is False
    assert health["capabilities"] == {"idempotency_keys": False, "lookup": "none"}
    assert "did not answer" in health["capability_source"]


def test_health_needs_settings_access_and_membership(stack: dict[str, Any]) -> None:
    st = stack
    assert get(st, "furn-investigator", f"{T}/connectors/health").status_code == 403
    other = get(st, "furn-admin", "/api/v1/tenants/demo-it/connectors/health")
    assert other.status_code in (403, 404)  # no membership in the IT organization
    assert "attempts_24h" not in other.text
