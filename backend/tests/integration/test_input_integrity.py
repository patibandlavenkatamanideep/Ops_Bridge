"""Source data that cannot be read completely never yields a match or a resolution
(production review A01), through the purchasing source, PostgreSQL and the API.

Before: an invoice whose lines were an object, null, text or an empty list matched (no line
was assessed, so nothing was found) and the case could then be resolved; an unreadable line
was dropped and the rest of the invoice matched; a receipt whose return line could not be
read still counted the goods as received. Now the record is not built, the document needs
review, and an investigation does not conclude while a document of the case needs review.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .stack import get, investigate, open_case
from .test_api_cases import call

pytestmark = pytest.mark.integration
ORG = "demo-it"
T = f"/api/v1/tenants/{ORG}"
WHO = "it-investigator"


def _latest(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    r = get(st, WHO, f"{T}/cases/{case_id}/investigation")
    assert r.status_code == 200, r.text
    return dict(r.json())


def _resolve(st: dict[str, Any], case_id: str) -> Any:
    return call(
        st["client"],
        st["actors"][WHO],
        "POST",
        f"{T}/cases/{case_id}/resolve",
        json={"note": "Matches"},
    )


def _with_items(original: bytes, items: Any) -> bytes:
    doc = json.loads(original)
    if items == "one_unreadable":
        good = doc["items"][0]
        items = [good, {k: v for k, v in good.items() if k != "quantity"} | {"lineId": "2"}]
    return json.dumps(doc | {"items": items}).encode()


@pytest.mark.parametrize(
    "items",
    [{}, None, "10 laptops", [], "one_unreadable"],
    ids=["object", "null", "string", "empty", "one_unreadable"],
)
def test_invoice_lines_that_cannot_be_read_neither_match_nor_resolve(stack, items: Any) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, WHO, ORG, "INV-7001")
    investigate(st, WHO, ORG, case_id)
    assert _latest(st, case_id)["outcome"] == "matched"  # 10 laptops, all received

    store = st["store"]
    original = store.get_record(ORG, "invoices", "INV-7001")
    assert original is not None
    store.put_record(
        ORG, "invoices", "INV-7001", _with_items(original[0], items), original[1], "PO-2001"
    )
    try:
        investigate(st, WHO, ORG, case_id)
    finally:
        store.put_record(ORG, "invoices", "INV-7001", original[0], original[1], "PO-2001")
    second = _latest(st, case_id)
    assert second["version"] == 2
    assert second["outcome"] == "manual_review", second["findings"]
    assert "invoice_extraction_needs_review" in {f["reason_code"] for f in second["findings"]}
    r = _resolve(st, case_id)
    assert r.status_code == 409 and r.json()["error"]["code"] == "resolution_evidence_missing"


def _return_receipt(uom: str | None) -> bytes:
    line: dict[str, Any] = {
        "poLineId": "1",
        "qtyReceived": "2",
        "movementType": "RMA",
        "condition": "Accepted",
    }
    if uom is not None:
        line["uom"] = uom
    return json.dumps(
        {
            "grnNumber": "GRN-6001B",
            "poRef": "PO-2001",
            "receivedDate": "2026-09-12",
            "items": [line],
        }
    ).encode()


@pytest.mark.parametrize(
    ("uom", "outcome", "reason"),
    [
        ("EA", "discrepancies_found", "invoiced_exceeds_received"),  # the return counts
        (None, "manual_review", "source_document_needs_review"),  # it cannot be read
    ],
    ids=["readable_return", "unreadable_return"],
)
def test_a_receipt_that_cannot_be_read_does_not_leave_the_goods_counted(
    stack, uom: str | None, outcome: str, reason: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    store = st["store"]
    store.put_record(
        ORG, "receipts", "GRN-6001B", _return_receipt(uom), "application/json", "PO-2001"
    )
    try:
        case_id = open_case(st, WHO, ORG, "INV-7001")
        investigate(st, WHO, ORG, case_id)
    finally:
        store.delete_record(ORG, "receipts", "GRN-6001B")
    result = _latest(st, case_id)
    assert result["outcome"] == outcome, result["findings"]
    assert reason in {f["reason_code"] for f in result["findings"]}
