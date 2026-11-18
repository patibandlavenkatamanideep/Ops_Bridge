"""Receiving and disposition on the stack: stored facts, history, and what an approval may do
once receiving evidence changes (docs/receiving-disposition.md).

Records come from the synthetic receiving pack (fixtures/receiving/disposition.yaml) or are
variants of its cases, put in the local simulator as the purchasing system holds them at each
step. Rules only; nothing reaches a live destination. The demo tenant's policy,
furniture-ap@v2, names no owner for rejected goods.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, select, text

from opsbridge.domain.receiving import precision
from opsbridge.evidence.ingest import ingest
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    ApprovalDecision,
    DocumentVersion,
    GoodsReceiptRecord,
    Operation,
    ReceiptLineRecord,
    ReceivingEventRecord,
    SourceDocument,
    Tenant,
)
from opsbridge.pilot.receiving_pack import Case, Event, event_records, load_pack, render_case
from opsbridge.services.execution_admission import refusal
from opsbridge.services.freshness import STALE

from .stack import get, open_case
from .test_api_cases import call
from .test_approvals_execution import (
    ORG,
    T,
    approve,
    drain,
    fault,
    operations,
    remote_tasks,
    sim,
    use_connector,
)

pytestmark = pytest.mark.integration
PACK = {c.id: c for c in load_pack().cases}
RULES_ONLY = {"model_mode": "rules_only"}


def put(st: dict[str, Any], records: list[Any], org: str = ORG) -> None:
    for collection, key, body, ctype, po in records:
        st["store"].put_record(org, collection, key, body, ctype, po)


def variant(case_id: str, new_id: str, **changes: Any) -> Case:
    """A pack case with its own numbers (so tests do not share records) and changes."""
    base = PACK[case_id]
    n = new_id
    number = base.po_number.removeprefix("PO-")  # 46xx; its receipt is GR-66xx
    raw = (
        base.model_dump_json()
        .replace(base.scenario.invoice.number, f"INV-{n}")
        .replace(f"GR-66{number[-2:]}", f"GR-{n}")
        .replace(number, n)  # the PO, and event ids and files named after it
    )
    case = Case.model_validate_json(raw)
    data = case.model_dump(mode="json")
    for path, value in changes.items():
        target = data
        *parents, leaf = path.split(".")
        for p in parents:
            target = target[int(p)] if p.isdigit() else target[p]
        target[int(leaf) if leaf.isdigit() else leaf] = value
    return Case.model_validate(data)


def investigate(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    who = st["actors"]["furn-investigator"]
    r = call(st["client"], who, "POST", f"{T}/cases/{case_id}/investigations", json=RULES_ONLY)
    assert r.status_code in (200, 202), r.text
    drain(st)
    return dict(get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json())


def opened(st: dict[str, Any], case: Case) -> tuple[str, dict[str, Any]]:
    put(st, render_case(case))
    case_id = open_case(st, "furn-investigator", ORG, case.scenario.invoice.number)
    return case_id, investigate(st, case_id)


def propose(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    r = call(
        st["client"], st["actors"]["furn-investigator"], "POST",
        f"{T}/cases/{case_id}/proposals", json={},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return dict(r.json())


def hold_dispatch(worker_db: Database, hold: bool = True) -> None:
    when = "now() + interval '1 hour'" if hold else "now()"
    with worker_db.session(DbContext()) as s:
        s.execute(
            text(
                f"UPDATE jobs SET run_after = {when} "
                "WHERE kind = 'dispatch_operation' AND status = 'queued'"
            )
        )


def tenant_id(engine: Engine, slug: str = ORG) -> uuid.UUID:
    with Database(engine).session(DbContext()) as s:
        return s.execute(select(Tenant.id).where(Tenant.slug == slug)).scalar_one()


def codes(iv: dict[str, Any]) -> list[str]:
    return sorted(f["reason_code"] for f in iv["findings"])


def finding(iv: dict[str, Any], reason_code: str) -> dict[str, Any]:
    [f] = [f for f in iv["findings"] if f["reason_code"] == reason_code]
    return dict(f)


# --- the two 18-unit cases -------------------------------------------------------------------


def test_the_two_18_unit_cases_keep_different_facts_citations_and_next_steps(
    stack: dict[str, Any], migrator_engine: Engine
) -> None:
    st = stack
    a_id, a = opened(st, PACK["rcv-a-short-delivery"])
    b_id, b = opened(st, PACK["rcv-b-rejected-damaged"])

    # Stored facts: B's receipt keeps the rejected row and its reason; A has no such row.
    tid = tenant_id(migrator_engine)
    with Database(migrator_engine).session(DbContext(tenant_id=tid)) as s:
        rows = {
            gr: sorted(
                (format(ln.quantity, "f"), ln.status, ln.reason)
                for ln in s.execute(
                    select(ReceiptLineRecord)
                    .join(
                        GoodsReceiptRecord,
                        GoodsReceiptRecord.id == ReceiptLineRecord.goods_receipt_id,
                    )
                    .where(
                        GoodsReceiptRecord.receipt_number == gr,
                        GoodsReceiptRecord.superseded_at.is_(None),
                    )
                ).scalars()
            )
            for gr in ("GR-6601", "GR-6602")
        }
    assert rows == {
        "GR-6601": [("18", "accepted", None)],
        "GR-6602": [("18", "accepted", None), ("2", "rejected", "damaged in transit")],
    }

    # A: two undelivered; receiving is asked about delivery.
    assert a["rules_version"] == "opsbridge-rules/1.4.0"
    assert (a["outcome"], codes(a)) == (
        "discrepancies_found",
        ["hold_unconfigured", "invoiced_exceeds_received"],
    )
    short = finding(a, "invoiced_exceeds_received")
    assert (short["values"]["delivered"], short["values"]["undelivered"]) == ("18", "2")
    assert (a["recommendation"]["route"], a["recommendation"]["assignee_group"]) == (
        "quantity_mismatch",
        "receiving",
    )

    # B: nothing undelivered; two rejected with no remedy; the policy names no owner.
    assert b["outcome"] == "manual_review"
    rejected = finding(b, "disposition_unrecorded")
    assert rejected["values"]["delivered"] == "20" and rejected["values"]["undelivered"] == "0"
    assert rejected["values"]["received_rejected"] == "2"
    assert rejected["values"]["rejection_reasons"] == "damaged in transit"
    fields = {e["field"] for e in rejected["evidence"]}
    assert {"receipt_line.reason", "receipt_line.status"} <= fields
    assert (
        "furniture-ap@v2 names no owner" in finding(b, "disposition_owner_unconfigured")["summary"]
    )
    assert (b["recommendation"]["decision"], b["recommendation"]["assignee_group"]) == (
        "escalate",
        "ap-review",
    )
    # The supplier's "all 20 delivered" agrees with receiving; it is not receipt evidence.
    assert "supplier_claim_includes_rejected" in codes(b)
    # Every citation resolves to the stored version it names.
    for iv in (a, b):
        assert iv["citations"] and all(c["status"] == "valid" for c in iv["citations"])
    _ = (a_id, b_id)


# --- a rejection learned after an approval -----------------------------------------------------


def late(case: Case, qty: str = "2", file: str | None = None) -> list[Any]:
    rej = Event(
        id=f"REJ-{case.po_number.removeprefix('PO-')}",
        event="REJECTED",
        qty=qty,
        receipt=case.receipts[0].id,
        occurred="2026-10-20",
        reason="damaged in transit",
        file=file or f"{case.po_number}-EVT-LATE",
    )
    return event_records(case.po_number, [rej])


def posted_for_twenty(n: str) -> Case:
    """Receipt for 20 posted as accepted; the invoice bills 22, so a follow-up is proposed."""
    return variant(
        "rcv-f-late-rejection",
        n,
        **{"events": [], "scenario.invoice.lines.0.qty": "22"},
    )


def test_a_rejection_learned_after_approval_stops_the_pending_follow_up(
    stack: dict[str, Any], worker_db: Database, migrator_engine: Engine
) -> None:
    st = stack
    case = posted_for_twenty("4701")
    case_id, first = opened(st, case)
    assert first["recommendation"]["route"] == "quantity_mismatch"
    prop = propose(st, case_id)
    assert prop["action_type"] == "create_follow_up_task"
    assert approve(st, case_id, prop).status_code == 200
    hold_dispatch(worker_db)

    # The purchasing system now records a rejection of 2, dated the delivery day.
    put(st, late(case))
    latest = investigate(st, case_id)
    hold_dispatch(worker_db, hold=False)
    drain(st)

    # Nothing was sent, and the approval does not carry over to the new evidence.
    [op] = operations(st, case_id)
    assert (op["status"], op["failure_category"]) == (
        "cancelled_before_dispatch",
        "precondition_failed",
    )
    assert op["attempts"] == [] and remote_tasks(st, op) == []
    proposals = get(st, "furn-investigator", f"{T}/cases/{case_id}/proposals").json()["items"]
    assert [p["status"] for p in proposals if p["id"] == prop["id"]] == ["superseded"]

    # The case is back for review under the current evidence: 18 accepted, 2 rejected.
    assert latest["version"] == first["version"] + 1
    rejected = finding(latest, "disposition_unrecorded")
    assert (rejected["values"]["received_accepted"], rejected["values"]["received_rejected"]) == (
        "18",
        "2",
    )

    # The original receipt and the approval remain as they were; the rejection is its own
    # record, dated before the approval and learned after it.
    tid = tenant_id(migrator_engine)
    with Database(migrator_engine).session(DbContext(tenant_id=tid)) as s:
        [gr] = s.execute(
            select(GoodsReceiptRecord).where(GoodsReceiptRecord.receipt_number == "GR-4701")
        ).scalars()
        assert gr.superseded_at is None
        [line] = s.execute(
            select(ReceiptLineRecord).where(ReceiptLineRecord.goods_receipt_id == gr.id)
        ).scalars()
        assert (format(line.quantity, "f"), line.status) == ("20", "accepted")
        [approval] = s.execute(
            select(ApprovalDecision).where(ApprovalDecision.proposal_id == uuid.UUID(prop["id"]))
        ).scalars()
        assert approval.decision == "approved"
        [rej] = s.execute(
            select(ReceivingEventRecord).where(ReceivingEventRecord.event_id == "REJ-4701")
        ).scalars()
        assert (rej.occurred, precision(rej.occurred)) == ("2026-10-20", "date")
        assert rej.occurred < approval.decided_at.date().isoformat()
        assert rej.created_at > approval.decided_at


def test_a_rejection_read_by_another_case_makes_the_approval_stale_at_both_boundaries(
    stack: dict[str, Any], worker_db: Database, mcp_db: Database, migrator_engine: Engine
) -> None:
    """The case itself is not investigated again: the rejection reaches OpsBridge as any
    other read of the PO would store it. The worker and the casework MCP tool admit writes
    with one function (services/execution_admission.py); both refuse."""
    st = stack
    case = posted_for_twenty("4702")
    case_id, _ = opened(st, case)
    prop = propose(st, case_id)
    assert approve(st, case_id, prop).status_code == 200
    hold_dispatch(worker_db)

    [(collection, key, body, ctype, _po)] = late(case)
    tid = tenant_id(migrator_engine)
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        res = ingest(
            s, st["deps"].store, tenant_id=tid, pack=st["deps"].pack_for("furniture"),
            record_type="receiving_event", source_key=f"purchasing:{collection}:{key}",
            origin="source_api", title=f"receiving event {key}", data=body, content_type=ctype,
        )  # fmt: skip
        assert res.new_version and res.records.get("receiving_events") == 1

    with worker_db.session(DbContext(tenant_id=tid)) as s:
        [op_row] = s.execute(
            select(Operation).where(Operation.case_id == uuid.UUID(case_id))
        ).scalars()
        op_id = op_row.id
        at_worker = refusal(s, op_row, outbound_writes_enabled=True)
    with mcp_db.session(DbContext(tenant_id=tid)) as s:
        mcp_op = s.get(Operation, op_id)
        assert mcp_op is not None
        at_mcp = refusal(s, mcp_op, outbound_writes_enabled=True, lock_case=False)
    for refused in (at_worker, at_mcp):
        assert refused is not None and refused.category == "precondition_failed"
        assert STALE in refused.problems, refused.problems

    hold_dispatch(worker_db, hold=False)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "cancelled_before_dispatch" and remote_tasks(st, op) == []


def test_an_identical_reimport_keeps_the_approval(
    stack: dict[str, Any], worker_db: Database
) -> None:
    st = stack
    case = variant("rcv-a-short-delivery", "4703")
    case_id, _ = opened(st, case)
    prop = propose(st, case_id)
    assert approve(st, case_id, prop).status_code == 200
    hold_dispatch(worker_db)
    put(st, render_case(case))  # the same records, byte for byte
    investigate(st, case_id)
    hold_dispatch(worker_db, hold=False)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed"
    assert len(remote_tasks(st, op)) == 1


# --- after the action was executed ---------------------------------------------------------


def test_a_late_rejection_after_a_confirmed_follow_up_keeps_it_and_returns_the_case(
    stack: dict[str, Any], migrator_engine: Engine
) -> None:
    st = stack
    case = posted_for_twenty("4704")
    case_id, _ = opened(st, case)
    prop = propose(st, case_id)
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    [sent] = operations(st, case_id)
    assert sent["status"] == "confirmed"
    [task] = remote_tasks(st, sent)

    put(st, late(case))
    latest = investigate(st, case_id)
    [op] = operations(st, case_id)
    # The execution and what it was based on stay as recorded; nothing is resent or undone.
    assert (op["status"], op["attempts"]) == ("confirmed", sent["attempts"])
    assert remote_tasks(st, op) == [task]
    assert "disposition_unrecorded" in codes(latest)
    case_now = get(st, "furn-investigator", f"{T}/cases/{case_id}").json()
    assert case_now["status"] == "awaiting_review"
    tid = tenant_id(migrator_engine)
    with Database(migrator_engine).session(DbContext(tenant_id=tid)) as s:
        events = s.execute(
            text(
                "SELECT details FROM audit_events WHERE action = "
                "'operation.evidence_changed_after_execution' AND resource_id = :op"
            ),
            {"op": op["id"]},
        ).scalars()
        [details] = list(events)
    assert details["status"] == "confirmed" and details["proposal_id"] == prop["id"]


def test_after_an_uncertain_outcome_a_late_rejection_leaves_the_review_path_in_charge(
    stack: dict[str, Any],
) -> None:
    st = stack
    use_connector(st, lambda: sim(st, idempotency_keys=False, lookup="none"), timeout_seconds=1.0)
    case = posted_for_twenty("4705")
    case_id, _ = opened(st, case)
    prop = propose(st, case_id)
    assert approve(st, case_id, prop).status_code == 200
    fault(st, "timeout_after_commit")
    drain(st)
    [unsure] = operations(st, case_id)
    assert (unsure["status"], unsure["review_required"]) == ("outcome_unknown", True)

    put(st, late(case))
    latest = investigate(st, case_id)
    drain(st)
    [op] = operations(st, case_id)
    assert (op["status"], op["review_required"]) == ("outcome_unknown", True)
    assert op["attempts"] == unsure["attempts"]  # no new attempt: nothing resent
    assert len(remote_tasks(st, op)) == 1
    assert "disposition_unrecorded" in codes(latest)


# --- repeat, corrected and out-of-order evidence --------------------------------------------


def test_a_corrected_event_row_is_a_new_revision_and_counts_once(
    stack: dict[str, Any], migrator_engine: Engine
) -> None:
    st = stack
    case = variant("rcv-f-late-rejection", "4706")
    case_id, first = opened(st, case)
    assert finding(first, "disposition_unrecorded")["values"]["received_rejected"] == "2"
    put(st, render_case(case))  # the same export again: nothing new
    again = investigate(st, case_id)
    assert finding(again, "disposition_unrecorded")["values"]["received_rejected"] == "2"

    corrected = variant("rcv-f-late-rejection", "4706", **{"events.0.qty": "3"})
    put(st, render_case(corrected))  # the same file and event, now for 3
    latest = investigate(st, case_id)
    assert finding(latest, "disposition_unrecorded")["values"]["received_rejected"] == "3"
    tid = tenant_id(migrator_engine)
    with Database(migrator_engine).session(DbContext(tenant_id=tid)) as s:
        rows = s.execute(
            select(ReceivingEventRecord.quantity, ReceivingEventRecord.superseded_at.is_(None))
            .where(ReceivingEventRecord.event_id == "REJ-4706")
            .order_by(ReceivingEventRecord.created_at)
        ).all()
        versions = s.execute(
            select(DocumentVersion.version_no)
            .join(SourceDocument, SourceDocument.id == DocumentVersion.document_id)
            .where(SourceDocument.source_key == "purchasing:receiving_events:PO-4706-EVT-1")
            .order_by(DocumentVersion.version_no)
        ).scalars()
        assert list(versions) == [1, 2]
    assert [(format(q, "f"), current) for q, current in rows] == [("2", False), ("3", True)]


def test_an_event_before_its_receipt_waits_and_applies_once_the_receipt_arrives(
    stack: dict[str, Any],
) -> None:
    st = stack
    case = PACK["rcv-g-rejection-before-its-receipt"]
    case_id, waiting = opened(st, case)
    assert (waiting["outcome"], codes(waiting)) == (
        "needs_information",
        ["event_reference_unresolved"],
    )
    assert "GR-6610" in finding(waiting, "event_reference_unresolved")["summary"]
    assert waiting["recommendation"]["assignee_group"] == "receiving"

    arrived = case.model_copy(
        update={
            "receipts": [
                PACK["rcv-f-late-rejection"].receipts[0].model_copy(update={"id": "GR-6610"})
            ]
        }
    )
    put(st, render_case(arrived))
    latest = investigate(st, case_id)
    applied = finding(latest, "disposition_unrecorded")
    assert (applied["values"]["received_accepted"], applied["values"]["received_rejected"]) == (
        "18",
        "2",
    )


def test_an_event_never_attaches_to_another_tenants_receipt(
    stack: dict[str, Any], worker_db: Database, migrator_engine: Engine
) -> None:
    """The IT tenant holds a receipt with the number the event names; the furniture tenant's
    investigation cannot see it and leaves the event unapplied."""
    st = stack
    it_tid = tenant_id(migrator_engine, "demo-it")
    it_receipt = (
        b'{"grnNumber": "GR-4707", "poRef": "PO-4707", "receivedDate": "2026-10-20", "items": '
        b'[{"poLineId": "1", "qtyReceived": "20", "uom": "EA", "movementType": "Inbound", '
        b'"condition": "Accepted"}]}'
    )
    with worker_db.session(DbContext(tenant_id=it_tid)) as s:
        res = ingest(
            s, st["deps"].store, tenant_id=it_tid, pack=st["deps"].pack_for("it_hardware"),
            record_type="goods_receipt", source_key="purchasing:receipts:GR-4707",
            origin="source_api", title="goods receipt GR-4707", data=it_receipt,
            content_type="application/json",
        )  # fmt: skip
        assert res.records.get("goods_receipts") == 1
    case = variant("rcv-g-rejection-before-its-receipt", "4707")
    assert case.events[0].receipt == "GR-4707"
    _case_id, iv = opened(st, case)
    assert iv["outcome"] == "needs_information"
    assert "GR-4707" in finding(iv, "event_reference_unresolved")["summary"]


# --- what a reviewer can inspect: the case's receiving view ---------------------------------


def view(st: dict[str, Any], case_id: str, who: str = "furn-investigator") -> dict[str, Any]:
    r = get(st, who, f"{T}/cases/{case_id}/receiving")
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_the_receiving_view_shows_the_two_18_unit_cases_side_by_side(
    stack: dict[str, Any],
) -> None:
    st = stack
    a_id, _ = opened(st, PACK["rcv-a-short-delivery"])
    b_id, _ = opened(st, PACK["rcv-b-rejected-damaged"])
    a, b = view(st, a_id), view(st, b_id)
    [la], [lb] = a["lines"], b["lines"]
    keys = ("ordered", "delivered", "accepted", "rejected", "undelivered", "disposition")
    assert [la[k] for k in keys] == ["20", "18", "18", "0", "2", "none"]
    assert [lb[k] for k in keys] == ["20", "20", "18", "2", "0", "unrecorded"]
    assert (la["matched"], la["disputed"]) == (lb["matched"], lb["disputed"]) == ("18", "2")
    [rej] = lb["rejections"]
    assert (rej["source"], rej["reason"], rej["occurred"], rej["late"]) == (
        "GR-6602",
        "damaged in transit",
        "2026-10-20",
        False,
    )
    assert {e["field"] for e in rej["evidence"]} >= {"receipt_line.reason", "receipt_line.status"}
    assert la["rejections"] == []
    # The next question, its owner and its basis, without model prose.
    assert (a["question"]["owner"], a["question"]["owner_configured"]) == ("receiving", True)
    assert [x["reason_code"] for x in a["question"]["basis"]] == ["invoiced_exceeds_received"]
    assert (b["question"]["owner"], b["question"]["owner_configured"]) == ("ap-review", False)
    assert [x["reason_code"] for x in b["question"]["basis"]] == [
        "disposition_owner_unconfigured",
        "disposition_unrecorded",
    ]
    for v in (a, b):
        assert v["split"]["hold"] == "unconfigured"
        assert "payment recommendation is left unresolved" in v["split"]["summary"]
        assert v["since_approval"] == []


def test_the_view_shows_what_changed_since_approval_and_when_it_was_learned(
    stack: dict[str, Any], worker_db: Database
) -> None:
    st = stack
    case = posted_for_twenty("4710")
    case_id, _ = opened(st, case)
    prop = propose(st, case_id)
    assert approve(st, case_id, prop).status_code == 200
    hold_dispatch(worker_db)
    put(st, late(case))
    investigate(st, case_id)
    hold_dispatch(worker_db, hold=False)
    drain(st)
    v = view(st, case_id)
    [since] = v["since_approval"]
    assert since["proposal_status"] == "superseded"
    assert since["operation_status"] == "cancelled_before_dispatch"
    assert since["requires"].startswith("review:")
    assert [(c["change"], c["kind"]) for c in since["changes"]] == [("added", "receiving_event")]
    kinds = [(e["kind"], e["id"]) for e in v["timeline"]]
    assert kinds.index(("approval", prop["id"])) < kinds.index(("event", "REJ-4710"))
    [rej_entry] = [e for e in v["timeline"] if e["id"] == "REJ-4710"]
    assert (rej_entry["occurred"], rej_entry["occurred_precision"]) == ("2026-10-20", "date")
    assert rej_entry["after_approval"] is True and rej_entry["current"] is True
    [gr_entry] = [e for e in v["timeline"] if e["id"] == "GR-4710"]
    assert gr_entry["after_approval"] is False and gr_entry["current"] is True
    assert "20 EA receipt accepted" in gr_entry["summary"]
    [line] = v["lines"]
    assert (line["accepted"], line["rejected"]) == ("18", "2")
    [rej] = line["rejections"]
    assert (rej["source"], rej["late"], rej["receipt_number"]) == ("REJ-4710", True, "GR-4710")


def test_after_execution_the_view_asks_for_reconciliation_not_a_resend(
    stack: dict[str, Any],
) -> None:
    st = stack
    case = posted_for_twenty("4711")
    case_id, _ = opened(st, case)
    prop = propose(st, case_id)
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    put(st, late(case))
    investigate(st, case_id)
    [since] = view(st, case_id)["since_approval"]
    assert since["operation_status"] == "confirmed"
    assert since["requires"] == (
        "reconciliation: the action was carried out on evidence that has since changed; "
        "nothing was repeated or undone"
    )


def test_the_view_shows_remedies_apart_from_movement(stack: dict[str, Any]) -> None:
    st = stack
    credited_id, _ = opened(st, PACK["rcv-c-credited"])
    replaced_id, _ = opened(st, PACK["rcv-d-replaced"])
    [c] = view(st, credited_id)["lines"]
    assert (c["accepted"], c["rejected"], c["returned"], c["credit_posted"]) == (
        "18",
        "2",
        "2",
        "2",
    )
    assert (c["disposition"], c["disputed"]) == ("credited", "0")
    [r] = view(st, replaced_id)["lines"]
    assert (r["ordered"], r["accepted"], r["rejected"]) == ("20", "18", "2")
    assert (r["replacement_delivered"], r["replacement_accepted"], r["covered"]) == ("2", "2", "20")
    assert r["disposition"] == "replaced"


def test_a_requester_cannot_read_receiving_evidence(stack: dict[str, Any]) -> None:
    st = stack
    case_id, _ = opened(st, PACK["rcv-a-short-delivery"])
    r = get(st, "furn-requester", f"{T}/cases/{case_id}/receiving")
    assert r.status_code in (403, 404), r.text


def test_an_export_carries_the_receiving_events_to_the_investigation(
    stack: dict[str, Any], tmp_path: Any
) -> None:
    from datetime import UTC, datetime, timedelta

    from opsbridge.connectors.exports import ExportSource
    from opsbridge.pilot.receiving_pack import load_pack, write_receiving_export

    st = stack
    write_receiving_export(load_pack(), tmp_path, datetime.now(UTC) - timedelta(minutes=5))
    deps = st["deps"]
    original = deps.purchasing_for
    deps.purchasing_for = lambda org: ExportSource(tmp_path / org)
    try:
        case_id = open_case(st, "furn-investigator", ORG, "INV-8609")
        iv = investigate(st, case_id)
    finally:
        deps.purchasing_for = original
    rejected = finding(iv, "disposition_unrecorded")
    assert (rejected["values"]["received_accepted"], rejected["values"]["received_rejected"]) == (
        "18",
        "2",
    )
    assert {"receiving_event.quantity", "receiving_event.reason"} <= {
        e["field"] for e in rejected["evidence"]
    }
