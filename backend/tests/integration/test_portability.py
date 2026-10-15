"""Portability (OB-03, Phase 9): the same core, configured only by each tenant's pack and
policy, investigates, cites, routes, proposes, approves and executes for both organizations.
Native formats differ (furniture: JSON/CSV/PDF; IT: camelCase JSON); routes and approvers come
from each policy and membership."""

from __future__ import annotations

from typing import Any

import pytest

from .stack import get, open_case
from .test_api_cases import call
from .test_approvals_execution import drain

pytestmark = pytest.mark.integration

PACKS = [
    pytest.param(
        {
            "tenant": "demo-furniture",
            "org": "demo-furniture",
            "invoice": "INV-9001",
            "investigator": "furn-investigator",
            "approver": "furn-approver",
            "group": "receiving",
            "policy": "furniture-ap@v2",
            "other": "it-investigator",
            "other_tenant": "demo-it",
            "formats": {"application/json", "text/csv", "application/pdf"},
        },
        id="furniture",
    ),
    pytest.param(
        {
            "tenant": "demo-it",
            "org": "demo-it",
            "invoice": "INV-7002",
            "investigator": "it-investigator",
            "approver": "it-approver",
            "group": "it-asset-intake",
            "policy": "it-procurement@v1",
            "other": "furn-investigator",
            "other_tenant": "demo-furniture",
            "formats": {"application/json"},
        },
        id="it_hardware",
    ),
]


@pytest.mark.parametrize("pack", PACKS)
def test_same_core_investigates_routes_and_executes_for_each_pack(
    stack, pack: dict[str, Any]
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    t = f"/api/v1/tenants/{pack['tenant']}"
    case_id = open_case(st, pack["investigator"], pack["tenant"], pack["invoice"])
    r = call(
        st["client"],
        st["actors"][pack["investigator"]],
        "POST",
        f"{t}/cases/{case_id}/investigations",
        json={"model_mode": "mock"},
    )
    assert r.status_code in (200, 202), r.text
    drain(st)
    inv = get(st, pack["investigator"], f"{t}/cases/{case_id}/investigation").json()
    assert inv["outcome"] == "discrepancies_found"
    assert {f["finding_type"] for f in inv["findings"]} >= {"quantity_mismatch"}
    assert inv["citations"] and all(c["status"] == "valid" for c in inv["citations"])
    assert inv["policy_ref"] == pack["policy"]
    rec = inv["recommendation"]
    assert (rec["action_type"], rec["assignee_group"]) == ("create_follow_up_task", pack["group"])
    docs = get(st, pack["investigator"], f"{t}/cases/{case_id}/documents").json()["items"]
    types = {v["content_type"] for d in docs for v in d["versions"]}
    assert types == pack["formats"]  # native formats differ; the core is the same

    prop = call(
        st["client"],
        st["actors"][pack["investigator"]],
        "POST",
        f"{t}/cases/{case_id}/proposals",
        json={},
    )
    assert prop.status_code == 201, prop.text
    p = prop.json()
    decided = call(
        st["client"],
        st["actors"][pack["approver"]],
        "POST",
        f"{t}/cases/{case_id}/proposals/{p['id']}/decision",
        json={
            "decision": "approve",
            "version": p["version"],
            "payload_digest": p["payload_digest"],
        },
    )
    assert decided.status_code == 200, decided.text
    drain(st)
    [op] = get(st, pack["approver"], f"{t}/cases/{case_id}/operations").json()["items"]
    assert op["status"] == "confirmed" and op["remote_ref"]
    tasks = st["store"].find_tasks(pack["org"], f"opsbridge-op:{op['operation_key']}")
    assert len(tasks) == 1  # created once, in this organization's task system

    other = f"/api/v1/tenants/{pack['other_tenant']}/cases/{case_id}"
    assert get(st, pack["other"], other).status_code == 404
    assert get(st, pack["other"], f"{t}/cases/{case_id}").status_code == 404


def test_roles_are_per_tenant_for_a_user_in_both(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    furn_case = open_case(st, "multi-member", "demo-furniture", "INV-9001")  # investigator here
    r = call(
        st["client"],
        st["actors"]["multi-member"],
        "POST",
        f"/api/v1/tenants/demo-furniture/cases/{furn_case}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (200, 202)
    it_case = open_case(st, "it-investigator", "demo-it", "INV-7002")
    r = call(
        st["client"],
        st["actors"]["multi-member"],
        "POST",
        f"/api/v1/tenants/demo-it/cases/{it_case}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (403, 404)  # only a requester in the IT organization
