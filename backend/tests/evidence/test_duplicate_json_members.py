"""A source document naming a member twice is refused whole (review N01).

Before: an invoice line giving `quantity` twice, 999999 and then 10, parsed and mapped with
no problem as 10, the decoder's last value, and so matched its purchase order and receipt.
Which of two values a source meant is not knowable, and the rendered document a reviewer
reads showed both. Now the document is rejected, no record is built from it, and the
mapping reports why; a document without duplicates parses as before.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from opsbridge.canonical import sha256_bytes
from opsbridge.config import REPO_ROOT
from opsbridge.evidence.parsers import parse
from opsbridge.mapping import DocRef, MappingResult, map_document
from opsbridge.packs import PackRegistry
from opsbridge.simulator.scenarios import load_corpus, render

PACKS = PackRegistry(REPO_ROOT / "packs")
SCENARIOS = load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml").scenarios
CORPUS = {s.id: s for s in SCENARIOS}
RECORD_TYPE = {
    "invoices": "invoice",
    "purchase_orders": "purchase_order",
    "receipts": "goods_receipt",
    "allocations": "allocation_history",
}


def _body(case_id: str, collection: str) -> bytes:
    [body] = [b for col, _k, b, _ct, _po in render(CORPUS[case_id]) if col == collection]
    return body


def _twice(body: bytes, member: str, first: Any) -> bytes:
    """`body` with `"member": first` inserted before the member's one occurrence."""
    text = json.dumps(json.loads(body))
    needle = f'"{member}": '
    assert text.count(needle) == 1, member
    return text.replace(needle, f"{needle}{json.dumps(first)}, {needle}").encode()


def _map(pack: str, record_type: str, body: bytes) -> MappingResult:
    ref = DocRef(str(uuid.uuid4()), str(uuid.uuid4()), sha256_bytes(body))
    return map_document(PACKS.get(pack), record_type, parse(body, "application/json"), ref)  # type: ignore[arg-type]


def test_a_line_quantity_given_twice_is_not_read_as_either_value() -> None:
    body = _twice(_body("it-dev-001", "invoices"), "quantity", 999999)
    assert b'"quantity": 999999, "quantity": "10"' in body
    doc = parse(body, "application/json")
    assert doc.status == "rejected" and doc.data is None and (doc.text, doc.lines) == ("", [])
    assert doc.detail == "invalid JSON: duplicate JSON member 'quantity'"
    m = _map("it_hardware", "invoice", body)
    assert m.invoices == [] and not m.ok
    assert m.problems == [
        "document not parsed: rejected (invalid JSON: duplicate JSON member 'quantity')"
    ]


@pytest.mark.parametrize(
    ("collection", "record_type", "member", "first"),
    [
        ("invoices", "invoice", "invoiceId", "INV-9999"),  # header
        ("invoices", "invoice", "state", "Paid"),  # status
        ("invoices", "invoice", "unitCost", "1.00"),  # amount
        ("invoices", "invoice", "items", []),  # the line list
        ("purchase_orders", "purchase_order", "quantity", "1"),
        ("receipts", "goods_receipt", "qtyReceived", "0"),
    ],
)
def test_a_member_given_twice_anywhere_builds_no_record(
    collection: str, record_type: str, member: str, first: Any
) -> None:
    body = _twice(_body("it-dev-001", collection), member, first)
    m = _map("it_hardware", record_type, body)
    assert not m.ok and not (m.invoices or m.purchase_orders or m.receipts)
    assert f"duplicate JSON member {member!r}" in m.problems[0]


def test_the_furniture_pack_refuses_it_too() -> None:
    body = _twice(_body("fur-dev-001", "purchase_orders"), "qty_ordered", "2000")
    m = _map("furniture", "purchase_order", body)
    assert m.purchase_orders == [] and not m.ok


def test_every_corpus_document_without_duplicates_parses_as_before() -> None:
    """Against an independent decoder: the standard library's, keeping fractional numbers
    as their source text the way the parser does."""
    checked = 0
    for scenario in SCENARIOS:
        for collection, _key, body, ctype, _po in render(scenario):
            if ctype != "application/json":
                continue
            doc = parse(body, "application/json")
            assert doc.status == "parsed", (scenario.id, collection, doc.detail)
            assert doc.data == json.loads(body, parse_float=str), (scenario.id, collection)
            checked += 1
    assert checked > 50
