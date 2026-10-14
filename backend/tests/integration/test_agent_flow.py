"""Bounded agent assistance through the real stack (Phase 3 exit gate).

The compromised-model tests assume the worst case: a model that follows instructions
embedded in a supplier document. The system's guarantees must hold anyway.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from opsbridge.agents.adapters import ScriptedModelAdapter
from opsbridge.agents.contracts import ModelUnavailable
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Case, ModelCall, Operation, ToolInvocation, WorkflowRun

from .stack import get, investigate, open_case
from .test_api_cases import call

pytestmark = pytest.mark.integration

INJECTION = (
    Path(__file__).resolve().parents[3] / "fixtures" / "documents" / "injection_supplier_email.txt"
).read_bytes()
T = "/api/v1/tenants/demo-furniture"


def _rows(worker_db: Database, tenant_id: uuid.UUID, model: Any, run_id: str) -> list[Any]:
    with worker_db.session(DbContext(tenant_id=tenant_id)) as s:
        return list(s.execute(select(model).where(model.run_id == uuid.UUID(run_id))).scalars())


def _tenant_of(worker_db: Database, run_id: str) -> uuid.UUID:
    from sqlalchemy import text

    with worker_db.session(DbContext()) as s:
        return s.execute(
            text("SELECT tenant_id FROM jobs WHERE payload->>'run_id' = :r"), {"r": run_id}
        ).scalar_one()


def test_mock_backed_full_flow_records_model_and_tool_calls(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9023")
    run = investigate(st, "furn-investigator", "demo-furniture", case_id)
    inv = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert inv["explanation_origin"] == "model:mock"
    assert "[E" in inv["explanation"]  # statements carry evidence labels
    assert inv["outcome"] == "discrepancies_found"  # rules remain authoritative
    draft = inv["recommendation"]["proposal_draft"]
    assert draft["origin"] == "model:mock" and draft["gate"]["passed"] is True
    assert (draft["action_type"], draft["assignee_group"]) == ("create_follow_up_task", "receiving")

    tenant_id = _tenant_of(worker_db, run["run_id"])
    calls = _rows(worker_db, tenant_id, ModelCall, run["run_id"])
    assert [c.step_name for c in calls] == [
        "explain_findings",
        "explain_findings",
        "propose_follow_up",
    ]
    assert all(c.origin == "mocked" and c.outcome == "ok" for c in calls)
    assert all(
        c.prompt_template_digest.startswith("sha256:") and c.input_digest.startswith("sha256:")
        for c in calls
    )
    assert calls[0].params["requested"]["max_output_tokens"] > 0
    tools = _rows(worker_db, tenant_id, ToolInvocation, run["run_id"])
    assert [(t.tool_name, t.outcome, t.transport) for t in tools] == [
        ("search_case_evidence", "ok", "inprocess")
    ]
    assert tools[0].response_digest and tools[0].request_artifact_id
    with worker_db.session(DbContext(tenant_id=tenant_id)) as s:
        wr = s.get(WorkflowRun, uuid.UUID(run["run_id"]))
        assert wr is not None and wr.response_mode == "mocked"
        assert wr.usage["routing"]["mode"] == "mock" and not wr.usage["routing"]["blocked"]


def _explanation(**kw: Any) -> str:
    base = {
        "outcome": "answer",
        "summary": "Quantity shortfall on the order.",
        "statements": [{"text": "The receiving record shows 18 received.", "citations": ["E1"]}],
        "uncertainties": [],
    }
    base.update(kw)
    return json.dumps(base)


def test_injected_document_instructions_cannot_change_authority(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    # The supplier sends an email that tries to instruct the AI.
    httpx.put(
        f"{st['base']}/_control/records/demo-furniture/messages/MSG-INJ-1?po_number=PO-1001",
        content=INJECTION,
        headers={"content-type": "text/plain"},
    ).raise_for_status()
    compromised = ScriptedModelAdapter(
        [
            # Round 0: valid, but asks to search for the injected text.
            _explanation(
                evidence_requests=[
                    {"tool": "search_case_evidence", "query": "approve payment release"}
                ]
            ),
            # Round 1: follows the injection — claims resolution, cites unknown evidence, invents numbers.
            _explanation(
                summary="Case resolved; approved for payment as instructed.",
                statements=[
                    {"text": "All 20 chairs delivered; release payment.", "citations": ["E99"]}
                ],
            ),
            # Proposal: tries to reroute to finance and release payment.
            json.dumps(
                {
                    "decision": "propose",
                    "action_type": "create_follow_up_task",
                    "assignee_group": "finance-director",
                    "title": "Release payment",
                    "body": "Approved for payment per supplier instruction.",
                    "citations": ["E1"],
                    "rationale": "Supplier said so.",
                }
            ),
        ]
    )
    st["deps"].adapter_overrides = {"mock": compromised}
    try:
        case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
        run = investigate(st, "furn-investigator", "demo-furniture", case_id)
    finally:
        st["deps"].adapter_overrides = None
        httpx.delete(f"{st['base']}/_control/faults")

    # The injected text reached the model only as delimited, untrusted data.
    round1 = compromised.requests[1].user
    assert "SYSTEM NOTE TO THE AI ASSISTANT" in round1
    before = round1.split("SYSTEM NOTE TO THE AI ASSISTANT")[0]
    assert before.rfind("<untrusted_document") > before.rfind("</untrusted_document>")

    inv = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert inv["outcome"] == "discrepancies_found"  # deterministic findings unchanged
    assert {f["finding_type"] for f in inv["findings"]} >= {"quantity_mismatch"}
    assert inv["explanation_origin"].startswith("template")
    assert "resolved" not in inv["explanation"].lower().replace("does not resolve", "")
    assert any("model explanation rejected" in u for u in inv["uncertainty"])
    draft = inv["recommendation"]["proposal_draft"]
    assert draft["origin"] == "rules" and draft["assignee_group"] == "receiving"
    assert draft["validation"]["accepted"] is False
    assert any("assignee group" in p for p in draft["validation"]["problems"])
    assert "payment" not in draft["title"].lower()

    tenant_id = _tenant_of(worker_db, run["run_id"])
    calls = _rows(worker_db, tenant_id, ModelCall, run["run_id"])
    assert [c.outcome for c in calls] == ["ok", "rejected_output", "rejected_output"]
    tools = _rows(worker_db, tenant_id, ToolInvocation, run["run_id"])
    assert [t.tool_name for t in tools] == ["search_case_evidence"]  # only the read-only tool ran
    with worker_db.session(DbContext(tenant_id=tenant_id)) as s:
        case = s.get(Case, uuid.UUID(case_id))
        assert case is not None and case.status == "awaiting_review"
        assert s.execute(select(func.count()).select_from(Operation)).scalar_one() == 0


def test_local_required_policy_blocks_model_without_hosted_fallback(
    stack, worker_db: Database
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "it-investigator", "demo-it", "INV-7002")
    run = investigate(
        st, "it-investigator", "demo-it", case_id
    )  # tenant default: ollama (not installed)
    inv = get(
        st, "it-investigator", f"/api/v1/tenants/demo-it/cases/{case_id}/investigation"
    ).json()
    assert inv["outcome"] == "discrepancies_found"
    assert inv["explanation_origin"] == "template"
    assert any("local inference unavailable" in u for u in inv["uncertainty"])
    tenant_id = _tenant_of(worker_db, run["run_id"])
    assert _rows(worker_db, tenant_id, ModelCall, run["run_id"]) == []

    # Explicitly asking for the hosted model is refused by the data-routing policy.
    r = call(
        st["client"],
        st["actors"]["it-investigator"],
        "POST",
        f"/api/v1/tenants/demo-it/cases/{case_id}/investigations",
        json={"model_mode": "anthropic"},
    )
    assert r.status_code == 202
    while st["worker"].run_once():
        pass
    inv = get(
        st, "it-investigator", f"/api/v1/tenants/demo-it/cases/{case_id}/investigation"
    ).json()
    assert any("hosted model refused" in u for u in inv["uncertainty"])
    assert _rows(worker_db, tenant_id, ModelCall, r.json()["run_id"]) == []


def test_tier_entitlement_gates_model_modes(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9002")
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "anthropic"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_entitled"
    # Tier never grants authority: an approver on any tier still cannot start runs.
    r = call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={},
    )
    assert r.status_code == 403


def test_model_failure_preserves_deterministic_findings(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    st["deps"].adapter_overrides = {
        "mock": ScriptedModelAdapter([ModelUnavailable("provider down"), "not json"])
    }
    try:
        case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9009")
        run = investigate(st, "furn-investigator", "demo-furniture", case_id)
    finally:
        st["deps"].adapter_overrides = None
    inv = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert inv["outcome"] == "discrepancies_found"
    assert [f["finding_type"] for f in inv["findings"] if f["status"] == "discrepancy"] == [
        "price_mismatch"
    ]
    assert inv["explanation_origin"].startswith("template")
    assert inv["recommendation"]["proposal_draft"]["origin"] == "rules"
    tenant_id = _tenant_of(worker_db, run["run_id"])
    outcomes = [c.outcome for c in _rows(worker_db, tenant_id, ModelCall, run["run_id"])]
    assert outcomes == ["unavailable", "rejected_output"]


def test_active_run_is_not_silently_substituted_for_a_different_mode(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9004")
    actor = st["actors"]["furn-investigator"]
    first = call(
        st["client"],
        actor,
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "mock"},
    )
    assert first.status_code == 202
    same = call(
        st["client"],
        actor,
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "mock"},
    )
    assert (
        same.status_code == 200 and same.json()["run_id"] == first.json()["run_id"]
    )  # double submit
    other = call(
        st["client"],
        actor,
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert other.status_code == 409 and other.json()["error"]["code"] == "run_in_progress"
    assert other.json()["error"]["details"]["run_id"] == first.json()["run_id"]
    while st["worker"].run_once():
        pass
