"""Invoice identity is scoped by supplier, and identifiers are not merged by punctuation
(review finding F07).

Two suppliers can legitimately issue the same invoice number. Records used to be keyed by
number alone per tenant, so the newer one superseded the other and a case could be matched
against another supplier's invoice. Intake also stripped punctuation (INV/123, INV.123 and
INV123 were one invoice) and returned the first case for a reused idempotency key even when
the request differed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from opsbridge.config import REPO_ROOT
from opsbridge.simulator.scenarios import _furniture_invoice_pdf, load_corpus

from .stack import get, investigate, open_case
from .test_api_cases import call
from .test_hardening import upload

pytestmark = pytest.mark.integration
ORG = "demo-furniture"
T = f"/api/v1/tenants/{ORG}"


def other_supplier_invoice(number: str) -> tuple[bytes, str]:
    """The same invoice number, issued by a different supplier against its own PO."""
    corpus = load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml")
    s = next(x for x in corpus.scenarios if x.invoice.number == number)
    inv = s.invoice.model_copy(
        update={
            "supplier_id": "SUP-OTHER",
            "supplier_name": "Other Office Supply",
            "po_number": "PO-7777",
        }
    )
    return _furniture_invoice_pdf(s.model_copy(update={"invoice": inv})), str(s.invoice.supplier_id)


def new_case(st: dict[str, Any], body: dict[str, Any], key: str | None = None) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key else {}
    return call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases",
        json=body,
        headers=headers,
    )


def latest(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    return dict(get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json())


def test_another_suppliers_invoice_with_the_same_number_does_not_replace_this_one(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    a = open_case(st, "furn-investigator", ORG, "INV-9001")  # supplier not named
    investigate(st, "furn-investigator", ORG, a)
    assert latest(st, a)["outcome"] == "discrepancies_found"

    pdf, own_supplier = other_supplier_invoice("INV-9001")
    b = new_case(st, {"title": "Invoice from Other Office Supply"})
    assert b.status_code == 201, b.text
    up = upload(
        st["client"],
        st["actors"]["furn-investigator"],
        b.json()["id"],
        "invoice",
        "inv.pdf",
        pdf,
        "application/pdf",
    )
    assert up.status_code == 201 and up.json()["parse_status"] == "parsed", up.text

    # The number alone now names two current invoices: case A must not pick one.
    investigate(st, "furn-investigator", ORG, a)
    second = latest(st, a)
    assert second["outcome"] == "manual_review", second["findings"]
    assert "invoice_identity_ambiguous" in {f["reason_code"] for f in second["findings"]}

    # A case that names the supplier matches that supplier's invoice and nothing else.
    c = new_case(
        st,
        {
            "title": "Invoice INV-9001 (Acme)",
            "invoice_number": "INV-9001",
            "supplier_id": own_supplier,
        },
    )
    assert c.status_code == 201, c.text
    assert c.json()["supplier_id"] == own_supplier
    investigate(st, "furn-investigator", ORG, c.json()["id"])
    third = latest(st, c.json()["id"])
    assert third["outcome"] == "discrepancies_found", third["findings"]
    assert "PO-7777" not in str(third["findings"])


def test_identifiers_keep_their_punctuation_and_near_duplicates_need_confirmation(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    first = new_case(st, {"title": "Slash", "invoice_number": " inv/123 "})
    assert first.status_code == 201 and first.json()["invoice_number"] == "INV/123", first.text
    for near in ("INV.123", "INV123", "inv 123"):
        r = new_case(st, {"title": "Near", "invoice_number": near})
        assert r.status_code == 409 and r.json()["error"]["code"] == "possible_duplicate", (
            near,
            r.text,
        )
        assert r.json()["error"]["details"]["existing_invoice_number"] == "INV/123"
    distinct = new_case(st, {"title": "Dot", "invoice_number": "INV.123", "confirm_distinct": True})
    assert distinct.status_code == 201 and distinct.json()["invoice_number"] == "INV.123"
    exact = new_case(st, {"title": "Again", "invoice_number": "INV/123", "confirm_distinct": True})
    assert exact.status_code == 409 and exact.json()["error"]["code"] == "duplicate_case"

    zeros = new_case(st, {"title": "Zeros", "invoice_number": "INV-0042"})
    assert zeros.status_code == 201
    r = new_case(st, {"title": "No zeros", "invoice_number": "INV-42"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "possible_duplicate"


def test_a_reused_idempotency_key_must_carry_the_same_request(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    body = {"title": "Receipt missing for PO-1001", "po_number": "PO-1001"}
    first = new_case(st, body, key="intake-7")
    again = new_case(st, body, key="intake-7")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    other = new_case(st, {**body, "title": "A different problem"}, key="intake-7")
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_key_reused"

    # A replay is recognised by its key and request even after the case learned its PO from
    # the invoice; another key for the same invoice is a duplicate, not a replay.
    inv = {"title": "Invoice INV-9001 blocked", "invoice_number": "INV-9001"}
    created = new_case(st, inv, key="form-1")
    assert created.status_code == 201
    investigate(st, "furn-investigator", ORG, created.json()["id"])
    case = get(st, "furn-investigator", f"{T}/cases/{created.json()['id']}").json()
    assert case["po_number"] == "PO-1001"
    replay = new_case(st, inv, key="form-1")
    assert replay.status_code == 200 and replay.json()["id"] == created.json()["id"]
    second_form = new_case(st, inv, key="form-2")
    assert (
        second_form.status_code == 409 and second_form.json()["error"]["code"] == "duplicate_case"
    )
