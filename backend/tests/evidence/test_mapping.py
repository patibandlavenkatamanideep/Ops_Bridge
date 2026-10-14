"""Pack mappings: different source shapes → the same canonical records (OB-03, OB-04)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from opsbridge.canonical import sha256_bytes
from opsbridge.config import REPO_ROOT
from opsbridge.evidence.parsers import parse
from opsbridge.mapping import DocRef, map_document
from opsbridge.packs import PackRegistry
from opsbridge.simulator.scenarios import load_corpus, render

PACKS = PackRegistry(REPO_ROOT / "packs")
CORPUS = {s.id: s for s in load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml").scenarios}
DOCS = Path(__file__).resolve().parents[3] / "fixtures" / "documents"


def _doc(body: bytes) -> DocRef:
    sha = sha256_bytes(body)
    return DocRef(str(uuid.uuid4()), str(uuid.uuid4()), sha)


def _records(case_id: str, collection: str):  # type: ignore[no-untyped-def]
    return [
        (body, ctype) for col, _k, body, ctype, _po in render(CORPUS[case_id]) if col == collection
    ]


def test_furniture_pdf_invoice_maps_with_line_locators() -> None:
    [(body, ctype)] = _records("fur-dev-001", "invoices")
    m = map_document(PACKS.get("furniture"), "invoice", parse(body, ctype), _doc(body))
    assert m.ok, m.problems
    [inv] = m.invoices
    assert (inv.invoice_number, inv.po_number, inv.supplier_id, inv.currency) == (
        "INV-9001",
        "PO-1001",
        "SUP-ACME",
        "USD",
    )
    [line] = inv.lines
    assert (line.quantity, line.unit_price, line.po_line_number) == (
        Decimal("20"),
        Decimal("185.00"),
        "1",
    )
    assert line.refs["invoice_line.quantity"].locator.startswith("pdf:page=1;line=")


def test_independently_produced_cups_pdf_maps_through_the_same_pack() -> None:
    body = (DOCS / "cupsfilter_text.pdf").read_bytes()
    m = map_document(PACKS.get("furniture"), "invoice", parse(body, "application/pdf"), _doc(body))
    assert m.ok, m.problems
    assert m.invoices[0].lines[0].quantity == Decimal("20")


def test_furniture_csv_receipts_group_by_receipt_with_row_locators() -> None:
    [(body, ctype)] = _records("fur-dev-003", "receipts")
    m = map_document(PACKS.get("furniture"), "goods_receipt", parse(body, ctype), _doc(body))
    assert m.ok, m.problems
    assert sorted((r.receipt_number, r.lines[0].quantity) for r in m.receipts) == [
        ("GR-5003A", Decimal("7")),
        ("GR-5003B", Decimal("3")),
    ]
    assert m.receipts[0].lines[0].refs["receipt_line.quantity"].locator.startswith("csv:row=")


def test_same_business_facts_from_different_source_shapes_map_identically() -> None:
    """Portability: furniture snake_case JSON and IT camelCase JSON → equal canonical PO fields."""
    fur_body, _ = _records("fur-dev-002", "purchase_orders")[0]
    it_body, _ = _records("it-dev-001", "purchase_orders")[0]
    fur = map_document(
        PACKS.get("furniture"),
        "purchase_order",
        parse(fur_body, "application/json"),
        _doc(fur_body),
    )
    it = map_document(
        PACKS.get("it_hardware"),
        "purchase_order",
        parse(it_body, "application/json"),
        _doc(it_body),
    )
    for m in (fur, it):
        assert m.ok, m.problems
        po = m.purchase_orders[0]
        assert po.status == "open" and po.lines[0].line_number == "1"
        assert isinstance(po.lines[0].quantity, Decimal)
    assert fur.purchase_orders[0].refs["purchase_order.supplier_id"].locator == "json:/supplier/id"
    assert (
        it.purchase_orders[0].refs["purchase_order.supplier_id"].locator
        == "json:/vendor/vendorCode"
    )


def test_unknown_enum_value_is_a_problem_not_a_default() -> None:
    body = b'{"po_number":"PO-1","supplier":{"id":"S","name":"N"},"currency":"USD","status":"ON_HOLD","lines":[]}'
    m = map_document(
        PACKS.get("furniture"), "purchase_order", parse(body, "application/json"), _doc(body)
    )
    assert not m.ok and any("ON_HOLD" in p for p in m.problems)
    assert m.purchase_orders == []


def test_inconsistent_pdf_amounts_mark_fields_needs_review_and_produce_no_record() -> None:
    [(body, ctype)] = _records("fur-dev-024", "invoices")
    m = map_document(PACKS.get("furniture"), "invoice", parse(body, ctype), _doc(body))
    assert m.invoices == []
    assert any("line amount" in p for p in m.problems)
    assert m.fields and all(f.status == "needs_review" for f in m.fields)


def test_supplier_claims_are_extracted_as_unconfirmed_assertions() -> None:
    body = (DOCS / "injection_supplier_email.txt").read_bytes()
    m = map_document(
        PACKS.get("furniture"), "supplier_message", parse(body, "text/plain"), _doc(body)
    )
    [a] = m.assertions
    assert (a.po_number, a.quantity, a.extraction_status) == (
        "PO-1001",
        Decimal("20"),
        "unconfirmed",
    )
    # Embedded instructions are just text: mapping produces only the delivery claim.
    assert [f.field_path for f in m.fields] == ["supplier_message.delivered_quantity"]
