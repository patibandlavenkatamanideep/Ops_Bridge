"""Receiving and disposition evidence at the import boundary (docs/receiving-disposition.md).

Source field names stay in the pack (packs/furniture/pack.yaml); what comes out is canonical:
a receipt line's rejection reason, and receiving events with a locator on every value read.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from opsbridge.canonical import sha256_bytes
from opsbridge.config import REPO_ROOT
from opsbridge.evidence.parsers import parse
from opsbridge.mapping import DocRef, FieldSpec, LinesSpec, MappingResult, PackSpec, map_document
from opsbridge.packs import PackRegistry
from opsbridge.simulator.scenarios import load_corpus, render

PACKS = PackRegistry(REPO_ROOT / "packs")
FURNITURE = PACKS.get("furniture")

RECEIPT_WITH_REASON = (
    b"receipt_id,po_ref,received_on,po_line,qty,uom,movement,status,reason\n"
    b"GR-6501,PO-4101,2026-10-20,1,18,EA,RECEIPT,ACCEPTED,\n"
    b"GR-6501,PO-4101,2026-10-20,1,2,EA,RECEIPT,REJECTED,damaged in transit\n"
)
EVENTS = (
    b"event_id,event,po_ref,po_line,qty,uom,occurred_on,receipt_id,follows,invoice,document,reason\n"
    b"REJ-1,REJECTED,PO-4101,1,2,EA,2026-10-20,GR-6501,,,,crushed carton\n"
    b"RTV-1,RETURNED,PO-4101,1,2,EA,2026-10-24T09:30:00+00:00,,REJ-1,,RMA-77,\n"
    b"CN-1,CREDIT_POSTED,PO-4101,1,2,EA,,,RTV-1,INV-8501,CN-77,\n"
    b"CP-1,CREDIT_PROMISED,PO-4101,1,,,,,RTV-1,,,\n"
)


def _map(record_type: str, body: bytes, pack: PackSpec = FURNITURE) -> MappingResult:
    doc = DocRef(str(uuid.uuid4()), str(uuid.uuid4()), sha256_bytes(body))
    return map_document(pack, record_type, parse(body, "text/csv"), doc)  # type: ignore[arg-type]


def test_a_rejection_reason_is_read_and_cited_on_its_receipt_line() -> None:
    m = _map("goods_receipt", RECEIPT_WITH_REASON)
    assert m.ok, m.problems
    [gr] = m.receipts
    accepted, rejected = gr.lines
    assert (accepted.status, accepted.reason) == ("accepted", None)
    assert (rejected.status, rejected.reason) == ("rejected", "damaged in transit")
    assert rejected.refs["receipt_line.reason"].locator == "csv:row=3;col=reason"
    assert "receipt_line.reason" not in accepted.refs


def test_receipts_exported_before_the_reason_column_still_map_unchanged() -> None:
    corpus = load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml")
    case = next(s for s in corpus.scenarios if s.id == "fur-dev-003")
    [(body, ctype)] = [(b, c) for col, _k, b, c, _p in render(case) if col == "receipts"]
    doc = DocRef(str(uuid.uuid4()), str(uuid.uuid4()), sha256_bytes(body))
    m = map_document(FURNITURE, "goods_receipt", parse(body, ctype), doc)
    assert m.ok, m.problems
    assert all(
        ln.reason is None and "receipt_line.reason" not in ln.refs for ln in m.receipts[0].lines
    )
    assert not [f for f in m.fields if f.field_path == "receipt_line.reason"]


def test_receiving_events_map_one_per_row_with_a_locator_on_every_value() -> None:
    m = _map("receiving_event", EVENTS)
    assert m.ok, m.problems
    rej, rtv, cn, cp = m.events
    assert (rej.event_id, rej.event_type, rej.receipt_number, rej.reason) == (
        "REJ-1",
        "rejection",
        "GR-6501",
        "crushed carton",
    )
    assert (rej.quantity, rej.unit, rej.occurred) == (Decimal("2"), "EA", "2026-10-20")
    assert (rtv.event_type, rtv.follows, rtv.document_number, rtv.occurred) == (
        "return",
        "REJ-1",
        "RMA-77",
        "2026-10-24T09:30:00+00:00",
    )
    assert (cn.event_type, cn.invoice_number, cn.document_number, cn.occurred) == (
        "credit_posted",
        "INV-8501",
        "CN-77",
        None,  # the source does not say when: unknown, not today
    )
    assert (cp.event_type, cp.quantity, cp.unit) == ("credit_promised", None, None)
    assert rej.refs["receiving_event.reason"].locator == "csv:row=2;col=reason"
    assert cn.refs["receiving_event.document_number"].locator == "csv:row=4;col=document"
    assert "receiving_event.occurred" not in cn.refs


def test_an_unknown_event_kind_is_a_problem_not_a_guess() -> None:
    body = EVENTS.splitlines()[0] + b"\nLOST-1,LOST_IN_TRANSIT,PO-4101,1,2,EA,,,,,,\n"
    m = _map("receiving_event", body)
    assert not m.events
    assert any("value map" in p for p in m.problems)


def test_an_occurrence_that_is_not_a_date_or_a_zoned_time_is_refused() -> None:
    for when in (b"20/10/2026", b"2026-10-20T09:30:00", b"2026-13-01"):
        body = (
            EVENTS.splitlines()[0] + b"\nREJ-9,REJECTED,PO-4101,1,2,EA," + when + b",GR-6501,,,,\n"
        )
        m = _map("receiving_event", body)
        assert not m.events, when
        assert any("occurred" in p for p in m.problems), (when, m.problems)


def test_the_same_event_twice_in_one_document_is_used_neither_time() -> None:
    row = b"REJ-1,REJECTED,PO-4101,1,2,EA,2026-10-20,GR-6501,,,,\n"
    m = _map("receiving_event", EVENTS.splitlines()[0] + b"\n" + row + row)
    assert not m.events
    assert any("appears 2 times" in p for p in m.problems)


def test_an_export_without_an_acceptance_status_gives_unstated_not_accepted() -> None:
    receipt = FURNITURE.sources["goods_receipt"]
    assert receipt.lines is not None
    lines = {k: v for k, v in receipt.lines.record.items() if k != "status"}
    old = FURNITURE.model_copy(
        update={
            "sources": {
                **FURNITURE.sources,
                "goods_receipt": receipt.model_copy(update={"lines": LinesSpec(record=lines)}),
            }
        }
    )
    body = (
        b"receipt_id,po_ref,received_on,po_line,qty,uom,movement\n"
        b"GR-6502,PO-4102,2026-10-20,1,20,EA,RECEIPT\n"
    )
    m = _map("goods_receipt", body, old)
    assert m.ok, m.problems
    [line] = m.receipts[0].lines
    assert (line.quantity, line.status) == (Decimal("20"), "unstated")
    _ = FieldSpec  # the pack model the mapping is written in


# A receipt row whose status is blank. Before rules 1.4.0 a status the mapping did not get
# defaulted to accepted; now it is unstated, which changes the case (it asks for the
# acceptance). These pin which exports that affects (docs/pilot/export-contract.md).
BLANK_STATUS = (
    b"receipt_id,po_ref,received_on,po_line,qty,uom,movement,status\n"
    b"GR-6503,PO-4103,2026-10-20,1,20,EA,RECEIPT,\n"
)


def _status_mapped(**changes: Any) -> PackSpec:
    """The furniture pack with its receipt status spec changed as given."""
    receipt = FURNITURE.sources["goods_receipt"]
    assert receipt.lines is not None
    lines = dict(receipt.lines.record)
    lines["status"] = lines["status"].model_copy(update=changes)
    return FURNITURE.model_copy(
        update={
            "sources": {
                **FURNITURE.sources,
                "goods_receipt": receipt.model_copy(update={"lines": LinesSpec(record=lines)}),
            }
        }
    )


def test_the_shipped_packs_require_a_receipt_status() -> None:
    for name in ("furniture", "it_hardware"):
        receipt = PACKS.get(name).sources["goods_receipt"]
        assert receipt.lines is not None
        spec = receipt.lines.record["status"]
        assert (spec.optional, spec.default) == (False, None), name


def test_a_blank_status_is_refused_by_a_shipped_pack_as_before() -> None:
    m = _map("goods_receipt", BLANK_STATUS)
    assert m.receipts == []
    assert "goods_receipt:GR-6503:row2: missing receipt_line.status" in m.problems


def test_a_blank_status_a_pack_marks_optional_is_now_unstated_not_accepted() -> None:
    m = _map("goods_receipt", BLANK_STATUS, _status_mapped(optional=True))
    assert m.ok, m.problems
    [line] = m.receipts[0].lines
    assert line.status == "unstated"
    assert "receipt_line.status" not in line.refs


def test_a_pack_can_state_its_receipts_are_accepted_and_it_is_cited_as_the_pack_default() -> None:
    m = _map("goods_receipt", BLANK_STATUS, _status_mapped(default="accepted"))
    assert m.ok, m.problems
    [line] = m.receipts[0].lines
    assert line.status == "accepted"
    assert line.refs["receipt_line.status"].locator == "pack:default"
    [status] = [f for f in m.fields if f.field_path == "receipt_line.status"]
    assert (status.method, status.locator) == ("pack_default", "pack:default")


def test_a_supplier_saying_everything_arrived_is_an_assertion_never_a_receipt() -> None:
    body = b"Hello,\nWe delivered all 20 chairs for PO-4101 on 20 October.\nRegards"
    doc = DocRef(str(uuid.uuid4()), str(uuid.uuid4()), sha256_bytes(body))
    m = map_document(FURNITURE, "supplier_message", parse(body, "text/plain"), doc)
    assert not m.receipts and not m.events
    [claim] = m.assertions
    assert (claim.claim, claim.quantity, claim.extraction_status) == (
        "delivered_quantity",
        Decimal("20"),
        "unconfirmed",
    )
