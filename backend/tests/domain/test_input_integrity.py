"""Malformed, incomplete or ambiguous canonical input never becomes a clean match
(production review A01, A02).

Before these checks an invoice with no lines matched (nothing was assessed, so nothing was
found), a PO with two lines named "1" kept whichever came last, a receipt listed twice was
counted twice, and statuses or kinds outside the supported set were read as the ordinary
case. `reconcile` now refuses to conclude from such input however the records were built,
and its arithmetic is exact for every value the input contract admits.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from opsbridge.domain.matching import reconcile
from opsbridge.domain.records import MatchInput

from .builders import inv_line, invoice, load_policy, po, po_line, r_line, receipt

POLICY = load_policy("furniture/policies/furniture-ap-v1.yaml")


def _reasons(data: MatchInput) -> tuple[str, set[str]]:
    result = reconcile(data, POLICY)
    return result.outcome, {f.reason_code for f in result.findings}


def _clean(**changes: object) -> MatchInput:
    """INV-9001 fully received at the PO price: matches as built."""
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    return dataclasses.replace(data, **changes)  # type: ignore[arg-type]


def test_the_clean_reference_case_matches() -> None:
    assert _reasons(_clean())[0] == "matched"


def test_an_invoice_without_lines_is_not_a_match() -> None:
    outcome, reasons = _reasons(_clean(invoice=invoice(())))
    assert outcome == "manual_review" and "record_lines_missing" in reasons


def test_an_invoice_of_charges_only_is_not_a_goods_match() -> None:
    charges = invoice((inv_line("1", None, "1", "40.00", kind="charge"),))
    outcome, reasons = _reasons(_clean(invoice=charges))
    assert outcome == "manual_review" and "goods_lines_absent" in reasons


@pytest.mark.parametrize(
    ("first", "second"),
    [("1", "1"), ("1a", "1A"), (" 1", "1")],
    ids=["same", "case_only", "whitespace_only"],
)
def test_po_lines_sharing_an_identity_go_to_review_in_either_order(first: str, second: str) -> None:
    # Line "1" at 999.00 next to line "1" at 185.00: before, the later one silently won, so
    # one order matched and the other found a price discrepancy.
    lines = (po_line(first, "20", "999.00"), po_line(second, "20", "185.00"))
    inv = invoice((inv_line("1", second.strip(), "20", "185.00"),))
    for order in (lines, lines[::-1]):
        outcome, reasons = _reasons(_clean(invoice=inv, purchase_orders=(po(lines=order),)))
        assert outcome == "manual_review" and "line_identity_duplicate" in reasons


def test_invoice_lines_sharing_an_identity_go_to_review() -> None:
    inv = invoice((inv_line("1", "1", "10", "185.00"), inv_line("1", "1", "10", "185.00")))
    outcome, reasons = _reasons(_clean(invoice=inv))
    assert outcome == "manual_review" and "line_identity_duplicate" in reasons


def test_a_receipt_listed_twice_is_not_counted_twice() -> None:
    # 20 invoiced, one receipt of 10 that appears twice: before, 20 counted as received.
    half = receipt((r_line("1", "10"),), number="GR-5001")
    outcome, reasons = _reasons(_clean(receipts=(half, half)))
    assert outcome == "manual_review" and "record_identity_duplicate" in reasons


def test_several_receipt_lines_for_one_po_line_are_still_allowed() -> None:
    split = receipt((r_line("1", "12"), r_line("1", "8")))
    assert _reasons(_clean(receipts=(split,)))[0] == "matched"


@pytest.mark.parametrize(
    "change",
    [
        "invoice_status_paid",
        "po_status_on_hold",
        "invoice_line_kind_tax",
        "receipt_kind_transfer",
        "receipt_status_pending",
    ],
)
def test_statuses_and_kinds_outside_the_supported_set_go_to_review(change: str) -> None:
    data = _clean()
    line = data.invoice.lines[0]
    if change == "invoice_status_paid":
        data = _clean(invoice=dataclasses.replace(data.invoice, status="paid"))
    elif change == "po_status_on_hold":
        data = _clean(purchase_orders=(dataclasses.replace(po(), status="on_hold"),))
    elif change == "invoice_line_kind_tax":
        data = _clean(invoice=invoice((line, inv_line("2", "1", "0", "0", kind="tax"))))
    else:
        field, value = ("kind", "transfer") if "kind" in change else ("status", "pending")
        bad = dataclasses.replace(r_line("1", "20"), **{field: value})
        data = _clean(receipts=(receipt((bad,)),))
    outcome, reasons = _reasons(data)
    assert outcome == "manual_review" and "status_unsupported" in reasons


@pytest.mark.parametrize(
    "discount", ["-5.00", "3700.01"], ids=["negative_discount", "discount_above_line"]
)
def test_a_discount_below_zero_or_above_the_line_goes_to_review(discount: str) -> None:
    # The same out-of-range discount on PO and invoice gave equal net prices: a match.
    data = _clean(
        invoice=invoice((inv_line("1", "1", "20", "185.00", discount=discount),)),
        purchase_orders=(po(lines=(po_line("1", "20", "185.00", discount=discount),)),),
    )
    outcome, reasons = _reasons(data)
    assert outcome == "manual_review" and "discount_unsupported" in reasons


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("1E+19"), Decimal("0.0000000000001"), 20.0, "20"],
    ids=["nan", "infinity", "too_large", "too_many_places", "float", "text"],
)
def test_quantities_outside_the_input_contract_go_to_review(value: object) -> None:
    line = dataclasses.replace(inv_line("1", "1", "20", "185.00"), quantity=value)
    outcome, reasons = _reasons(_clean(invoice=invoice((line,))))
    assert outcome == "manual_review" and "value_unsupported" in reasons


@pytest.mark.parametrize("identifier", ["", "   ", "L" * 33], ids=["empty", "blank", "too_long"])
def test_line_identifiers_must_be_present_and_fit(identifier: str) -> None:
    line = dataclasses.replace(inv_line("1", "1", "20", "185.00"), line_number=identifier)
    outcome, reasons = _reasons(_clean(invoice=invoice((line,))))
    assert outcome == "manual_review" and "identifier_invalid" in reasons


def test_arithmetic_is_exact_at_the_largest_supported_values() -> None:
    # 18 integer and 12 fractional digits are accepted; Python's default 28-digit context
    # rounded both totals to the same value, hiding a 0.000000000001 excess.
    received = "123456789012345678.123456789012"
    invoiced = "123456789012345678.123456789013"
    data = _clean(
        invoice=invoice((inv_line("1", "1", invoiced, "1.00"),)),
        purchase_orders=(po(lines=(po_line("1", invoiced, "1.00"),)),),
        receipts=(receipt((r_line("1", received),)),),
    )
    outcome, reasons = _reasons(data)
    assert outcome == "discrepancies_found" and "invoiced_exceeds_received" in reasons
