"""The pack mapping builds no record from incomplete or ambiguous source data (review A01, A02).

Before: invoice lines given as an object, null or a string were read as no lines, with no
problem reported; an unreadable line was dropped and the rest of the invoice built; two PO
lines with one identity were both built (the matcher then kept one). Each record now passes
the canonical checks (domain/validation.py) or is not built, and the reason is reported, so
the document is marked as needing review and older extractions of it stop being current.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from opsbridge.canonical import sha256_bytes
from opsbridge.config import REPO_ROOT
from opsbridge.evidence.parsers import parse
from opsbridge.mapping import DocRef, MappingResult, PackSpec, map_document
from opsbridge.packs import PackRegistry
from opsbridge.simulator.scenarios import load_corpus, render

PACKS = PackRegistry(REPO_ROOT / "packs")
CORPUS = {s.id: s for s in load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml").scenarios}


def _source(case_id: str, collection: str) -> dict[str, Any]:
    [body] = [b for col, _k, b, _ct, _po in render(CORPUS[case_id]) if col == collection]
    return dict(json.loads(body))


def _map(pack: PackSpec | str, record_type: str, doc: dict[str, Any]) -> MappingResult:
    body = json.dumps(doc).encode()
    spec = PACKS.get(pack) if isinstance(pack, str) else pack
    ref = DocRef(str(uuid.uuid4()), str(uuid.uuid4()), sha256_bytes(body))
    return map_document(spec, record_type, parse(body, "application/json"), ref)  # type: ignore[arg-type]


def test_the_reference_it_invoice_maps() -> None:
    m = _map("it_hardware", "invoice", _source("it-dev-001", "invoices"))
    assert m.ok, m.problems
    assert len(m.invoices) == 1 and len(m.invoices[0].lines) == 1


@pytest.mark.parametrize(
    "items", [{}, None, "1 laptop", []], ids=["object", "null", "string", "empty"]
)
def test_invoice_lines_that_are_not_a_list_of_lines_build_no_invoice(items: object) -> None:
    doc = _source("it-dev-001", "invoices") | {"items": items}
    m = _map("it_hardware", "invoice", doc)
    assert m.invoices == [] and not m.ok
    assert any("line" in p for p in m.problems), m.problems


def test_an_unreadable_line_is_not_dropped_from_an_otherwise_valid_invoice() -> None:
    doc = _source("it-dev-001", "invoices")
    good = doc["items"][0]
    bad = {k: v for k, v in good.items() if k != "quantity"} | {"lineId": "2"}
    m = _map("it_hardware", "invoice", doc | {"items": [good, bad]})
    assert m.invoices == []
    assert any("missing invoice_line.quantity" in p for p in m.problems), m.problems


def test_po_lines_sharing_an_identity_build_no_purchase_order() -> None:
    doc = _source("it-dev-001", "purchase_orders")
    line = doc["items"][0]
    m = _map("it_hardware", "purchase_order", doc | {"items": [line, line | {"unitCost": "1.00"}]})
    assert m.purchase_orders == []
    assert any("identified as" in p for p in m.problems), m.problems


def test_an_identifier_too_long_to_store_builds_no_record() -> None:
    doc = _source("it-dev-001", "invoices")
    doc["items"][0]["lineId"] = "L" * 33  # the column holds 32
    m = _map("it_hardware", "invoice", doc)
    assert m.invoices == [] and any("longer than 32" in p for p in m.problems), m.problems


def test_a_record_repeated_in_one_document_builds_neither_copy() -> None:
    # Stored one after the other, the second would silently supersede the first.
    spec = PACKS.get("it_hardware")
    receipts = spec.sources["goods_receipt"].model_copy(update={"root": "receipts"})
    pack = spec.model_copy(update={"sources": {**spec.sources, "goods_receipt": receipts}})
    grn = _source("it-dev-001", "receipts")
    m = _map(pack, "goods_receipt", {"receipts": [grn, grn | {"items": grn["items"][:1]}]})
    assert m.receipts == []
    assert any("appears 2 times" in p for p in m.problems), m.problems
