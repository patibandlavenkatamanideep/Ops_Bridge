"""End-to-end deterministic investigation through the real stack (Phase 2 exit gate).

Simulator over real HTTP → connector → ingestion (artifact store, versions, chunks,
fields, records) → PostgreSQL with RLS → worker steps → investigation version → API.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select, text

from opsbridge.canonical import sha256_bytes
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import DocumentVersion, WorkflowStep
from opsbridge.workflow import jobs
from opsbridge.workflow.investigation import _run_step

from .stack import get, investigate, open_case, put_scenario, rehearsal
from .test_api_cases import call

pytestmark = pytest.mark.integration


def test_twenty_ordered_eighteen_received_produces_cited_quantity_mismatch(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
    run = investigate(st, "furn-investigator", "demo-furniture", case_id)
    status = get(
        st,
        "furn-investigator",
        f"/api/v1/tenants/demo-furniture/cases/{case_id}/runs/{run['run_id']}",
    ).json()
    assert status["status"] == "completed"
    assert [s["name"] for s in status["steps"]] == [
        "collect_context",
        "retrieve_policy",
        "reconcile_records",
        "explain_findings",
        "propose_follow_up",
        "validate_proposal",
    ]
    inv = get(
        st, "furn-investigator", f"/api/v1/tenants/demo-furniture/cases/{case_id}/investigation"
    ).json()
    assert inv["outcome"] == "discrepancies_found"
    assert inv["policy_ref"] == "furniture-ap@v2"
    [qty] = [f for f in inv["findings"] if f["finding_type"] == "quantity_mismatch"]
    assert qty["values"]["excess"] == "2" and qty["values"]["net_received"] == "18"
    # Every citation resolves to the exact source version with a matching digest.
    assert inv["citations"] and all(c["status"] == "valid" for c in inv["citations"])
    locators = {c["field"]: c["locator"] for c in inv["citations"]}
    assert locators["invoice_line.quantity"].startswith("pdf:page=1;line=")
    assert locators["receipt_line.quantity"].startswith("csv:row=")
    assert locators["po_line.quantity"] == "json:/lines/0/qty_ordered"
    receipt_cite = next(c for c in inv["citations"] if c["field"] == "receipt_line.quantity")
    assert receipt_cite["value"] == "18" and "18" in receipt_cite["excerpt"]
    assert inv["recommendation"]["action_type"] == "create_follow_up_task"
    assert "does not resolve" in inv["explanation"]
    case = get(st, "furn-investigator", f"/api/v1/tenants/demo-furniture/cases/{case_id}").json()
    assert case["status"] == "awaiting_review" and case["discrepancy_types"] == [
        "quantity_mismatch"
    ]
    assert case["status"] != "resolved"

    # Original bytes are preserved and their digest matches the manifest.
    docs = get(
        st, "furn-investigator", f"/api/v1/tenants/demo-furniture/cases/{case_id}/documents"
    ).json()["items"]
    assert {d["kind"] for d in docs} >= {"invoice", "purchase_order", "goods_receipt"}
    invoice_doc = next(d for d in docs if d["kind"] == "invoice")
    v = invoice_doc["versions"][0]
    raw = get(
        st,
        "furn-investigator",
        f"/api/v1/tenants/demo-furniture/cases/{case_id}/documents/versions/{v['version_id']}/original",
    )
    assert raw.status_code == 200 and sha256_bytes(raw.content) == v["raw_sha256"]
    assert raw.headers["content-disposition"].startswith("attachment")
    assert (
        v["normalization_version"] == "opsbridge-norm/1"
        and v["normalized_sha256"] != v["raw_sha256"]
    )


def test_pilot_scenario_a_delivered_is_not_accepted(stack) -> None:  # type: ignore[no-untyped-def]
    """20 chairs billed; receiving accepted 18 and rejected 2 as damaged; the supplier says all
    20 were delivered. All three are true. The operator sees each passage and quantity, why
    arrival does not clear the invoice, what stays open, and who the policy sends it to."""
    st = stack
    put_scenario(st, rehearsal("pilot-a"))
    t = "/api/v1/tenants/demo-furniture"
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-8401")
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{t}/cases/{case_id}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (200, 202), r.text
    while st["worker"].run_once():
        pass
    inv = get(st, "furn-investigator", f"{t}/cases/{case_id}/investigation").json()
    assert (inv["outcome"], inv["rules_version"]) == (
        "discrepancies_found",
        "opsbridge-rules/1.3.0",
    )
    [qty] = [f for f in inv["findings"] if f["finding_type"] == "quantity_mismatch"]
    assert {
        k: qty["values"][k] for k in ("invoiced", "received_accepted", "received_rejected")
    } == {
        "invoiced": "20",
        "received_accepted": "18",
        "received_rejected": "2",
    }
    assert "2 more were delivered and rejected" in qty["summary"]
    [claim] = [f for f in inv["findings"] if f["reason_code"] == "supplier_claim_includes_rejected"]
    assert "agrees with receiving" in claim["summary"]
    assert not [f for f in inv["findings"] if f["finding_type"] == "source_conflict"]

    # Every passage resolves to its source version: both receipt rows with their status, the
    # invoice's 20 and the supplier's own sentence.
    assert inv["citations"] and all(c["status"] == "valid" for c in inv["citations"])
    rows = {
        (c["field"], c["value"]) for c in inv["citations"] if c["field"].startswith("receipt_line")
    }
    assert {
        ("receipt_line.quantity", "18"),
        ("receipt_line.quantity", "2"),
        ("receipt_line.status", "rejected"),
    } <= rows
    invoice_qty = next(c for c in inv["citations"] if c["field"] == "invoice_line.quantity")
    assert invoice_qty["value"] == "20"
    [said] = [c for c in inv["citations"] if c["field"].startswith("supplier_message")]
    assert "delivered all 20 chairs" in said["excerpt"]

    # What stays open goes where policy furniture-ap@v2 routes a quantity mismatch; nothing is
    # credited, replaced or paid, and the case is not resolved.
    rec = inv["recommendation"]
    assert (rec["action_type"], rec["assignee_group"]) == ("create_follow_up_task", "receiving")
    case = get(st, "furn-investigator", f"{t}/cases/{case_id}").json()
    assert case["status"] == "awaiting_review"


def test_missing_receipt_is_needs_information_without_asserting_non_delivery(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9013")
    investigate(st, "furn-investigator", "demo-furniture", case_id)
    inv = get(
        st, "furn-investigator", f"/api/v1/tenants/demo-furniture/cases/{case_id}/investigation"
    ).json()
    assert inv["outcome"] == "needs_information"
    assert inv["evidence_readiness"] == "incomplete"
    reasons = {f["reason_code"] for f in inv["findings"]}
    assert {"receiving_record_unavailable", "supplier_claim_unverified"} <= reasons
    assert "does not establish whether the goods were delivered" in inv["explanation"]
    assert "not delivered" not in inv["explanation"].replace(
        "does not establish whether the goods were delivered", ""
    )
    case = get(st, "furn-investigator", f"/api/v1/tenants/demo-furniture/cases/{case_id}").json()
    assert case["status"] == "needs_information"


def test_rerun_reuses_unchanged_versions_and_correction_changes_outcome(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    t = "/api/v1/tenants/demo-furniture"
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9023")
    investigate(st, "furn-investigator", "demo-furniture", case_id)
    first = get(st, "furn-investigator", f"{t}/cases/{case_id}/investigation").json()
    conflict = next(f for f in first["findings"] if f["finding_type"] == "source_conflict")
    assert conflict["values"]["controlling_source"] == "goods_receipt"

    # Resolution is refused while the evidence still shows a discrepancy.
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{t}/cases/{case_id}/resolve",
        json={},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "resolution_evidence_missing"

    # Re-running without source changes creates a new investigation version but no new document versions.
    investigate(st, "furn-investigator", "demo-furniture", case_id)
    second = get(st, "furn-investigator", f"{t}/cases/{case_id}/investigation").json()
    assert (
        second["version"] == 2
        and second["source_manifest_digest"] == first["source_manifest_digest"]
    )

    # A human corrects the receiving quantity (e.g. a keying error confirmed with the warehouse).
    docs = get(st, "furn-investigator", f"{t}/cases/{case_id}/documents").json()["items"]
    gr = next(d for d in docs if d["kind"] == "goods_receipt")
    detail = get(
        st,
        "furn-investigator",
        f"{t}/cases/{case_id}/documents/versions/{gr['versions'][0]['version_id']}",
    ).json()
    fld = next(f for f in detail["fields"] if f["field_path"] == "receipt_line.quantity")
    stale = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{t}/cases/{case_id}/fields/{fld['field_id']}/corrections",
        json={
            "new_value": "20",
            "reason": "Warehouse recount confirmed 20 chairs",
            "expected_current_value": "17",
        },
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_version"
    denied = call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{t}/cases/{case_id}/fields/{fld['field_id']}/corrections",
        json={"new_value": "20", "reason": "Approvers may not edit evidence"},
    )
    assert denied.status_code == 403
    ok = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{t}/cases/{case_id}/fields/{fld['field_id']}/corrections",
        json={
            "new_value": "20",
            "reason": "Warehouse recount confirmed 20 chairs",
            "expected_current_value": "18",
        },
    )
    assert ok.status_code == 201, ok.text
    detail = get(
        st,
        "furn-investigator",
        f"{t}/cases/{case_id}/documents/versions/{gr['versions'][0]['version_id']}",
    ).json()
    fld = next(f for f in detail["fields"] if f["field_path"] == "receipt_line.quantity")
    assert (fld["original_value"], fld["current_value"]) == ("18", "20")  # original preserved
    assert fld["corrections"][0]["reason"].startswith("Warehouse recount")

    investigate(st, "furn-investigator", "demo-furniture", case_id)
    third = get(st, "furn-investigator", f"{t}/cases/{case_id}/investigation").json()
    assert third["outcome"] == "matched"
    assert (
        third["source_manifest_digest"] != second["source_manifest_digest"]
    )  # correction is bound
    assert any("corrected value used" in u for u in third["uncertainty"])
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{t}/cases/{case_id}/resolve",
        json={},
    )
    assert r.status_code == 200 and r.json()["status"] == "resolved"


def test_unreachable_source_is_retried_not_treated_as_missing(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    httpx.post(
        f"{st['base']}/_control/faults",
        json={"org": "demo-furniture", "route": "records.get", "mode": "fail_500", "count": 1},
    )
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9002")
    run = investigate(st, "furn-investigator", "demo-furniture", case_id)
    t = "/api/v1/tenants/demo-furniture"
    status = get(st, "furn-investigator", f"{t}/cases/{case_id}/runs/{run['run_id']}").json()
    assert status["status"] == "running"  # failed step, job re-queued with backoff
    assert status["steps"][0]["status"] == "failed" and "unavailable" in status["steps"][0]["error"]
    assert get(st, "furn-investigator", f"{t}/cases/{case_id}/investigation").status_code == 404
    with worker_db.session(DbContext()) as s:  # skip the backoff delay for the test
        s.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'queued'"))
    while st["worker"].run_once():
        pass
    inv = get(st, "furn-investigator", f"{t}/cases/{case_id}/investigation").json()
    assert inv["outcome"] == "matched"


def test_worker_restart_resumes_from_durable_steps(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9004")
    run = investigate(st, "furn-investigator", "demo-furniture", case_id, drain=False)
    job = jobs.claim(worker_db, "crashing-worker", 1)
    assert job is not None
    # Simulate a worker that completed collect_context and then died (lease left to expire).
    from opsbridge.persistence.models import WorkflowRun

    with worker_db.session(DbContext(tenant_id=job.tenant_id)) as s:
        wr = s.get(WorkflowRun, uuid.UUID(run["run_id"]))
        assert wr is not None
    deps = st["deps"]
    from opsbridge.workflow.investigation import _collect, _ctx

    with worker_db.session(_ctx(job.tenant_id)) as s:
        wr = s.get(WorkflowRun, uuid.UUID(run["run_id"]))
        s.expunge(wr)
    pack = deps.pack_for("furniture")
    snapshot = {
        "case_id": case_id,
        "invoice_number": "INV-9004",
        "po_number": None,
        "case_number": "x",
    }
    _run_step(
        deps,
        wr,
        jobs.Lease(worker_db, job, 1),  # the dying worker still held its lease here
        1,
        "collect_context",
        snapshot,
        lambda: _collect(deps, job.tenant_id, "demo-furniture", pack, snapshot),
    )
    with worker_db.session(_ctx(job.tenant_id)) as s:
        first_started = s.execute(
            select(WorkflowStep.started_at).where(
                WorkflowStep.run_id == wr.id, WorkflowStep.step_name == "collect_context"
            )
        ).scalar_one()
        versions_before = s.execute(select(DocumentVersion.id)).scalars().all()
    with worker_db.session(DbContext()) as s:  # lease expiry
        s.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job.id},
        )
    assert st["worker"].run_once()  # a new worker claims the expired lease and resumes
    with worker_db.session(_ctx(job.tenant_id)) as s:
        steps = {
            r.step_name: r
            for r in s.execute(select(WorkflowStep).where(WorkflowStep.run_id == wr.id)).scalars()
        }
        assert steps["collect_context"].started_at == first_started  # not re-run
        assert all(x.status == "completed" for x in steps.values())
        assert sorted(s.execute(select(DocumentVersion.id)).scalars().all()) == sorted(
            versions_before
        )
    # The crashed worker can no longer complete the job: its fencing token is stale.
    assert jobs.complete(worker_db, job) is False


def test_evidence_is_tenant_and_permission_scoped(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-requester", "demo-furniture", "INV-9010")
    investigate(st, "furn-investigator", "demo-furniture", case_id)
    t = "/api/v1/tenants/demo-furniture"
    # Requester sees the outcome but not evidence locations, values or documents.
    inv = get(st, "furn-requester", f"{t}/cases/{case_id}/investigation").json()
    assert (
        inv["outcome"] == "discrepancies_found"
        and inv["citations"] == []
        and not inv["evidence_visible"]
    )
    assert all("evidence" not in f and "values" not in f for f in inv["findings"])
    assert get(st, "furn-requester", f"{t}/cases/{case_id}/documents").status_code == 403
    # Tenant admin: queue metadata only, no document content.
    assert get(st, "furn-admin", f"{t}/cases/{case_id}/documents").status_code == 403
    # Another tenant: nothing, via either tenant scope.
    docs = get(st, "furn-investigator", f"{t}/cases/{case_id}/documents").json()["items"]
    vid = docs[0]["versions"][0]["version_id"]
    for url in (
        f"/api/v1/tenants/demo-it/cases/{case_id}/documents",
        f"/api/v1/tenants/demo-it/cases/{case_id}/documents/versions/{vid}/original",
        f"/api/v1/tenants/demo-furniture/cases/{case_id}/documents/versions/{vid}/original",
    ):
        r = get(st, "it-investigator", url)
        assert r.status_code == 404, (url, r.status_code)
    # Starting an investigation requires investigation.run.
    r = call(
        st["client"], st["actors"]["furn-approver"], "POST", f"{t}/cases/{case_id}/investigations"
    )
    assert r.status_code == 403


def test_it_pack_runs_through_the_same_core(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "it-investigator", "demo-it", "INV-7007")
    investigate(st, "it-investigator", "demo-it", case_id)
    inv = get(
        st, "it-investigator", f"/api/v1/tenants/demo-it/cases/{case_id}/investigation"
    ).json()
    assert inv["policy_ref"] == "it-procurement@v1"
    assert inv["outcome"] == "manual_review"
    assert {f["reason_code"] for f in inv["findings"]} >= {
        "source_precedence_undefined",
        "invoiced_exceeds_received",
    }
    assert all(c["status"] == "valid" for c in inv["citations"])
    assert any(c["locator"].startswith("json:/items/") for c in inv["citations"])
