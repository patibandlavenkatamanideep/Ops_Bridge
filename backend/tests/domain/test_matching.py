"""Matcher behaviour against hand-computed expectations (acceptance scenarios §19)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opsbridge.domain.decision import recommend
from opsbridge.domain.matching import MatchResult, reconcile
from opsbridge.domain.records import MatchInput, Supplier

from .builders import (
    allocation,
    assertion,
    inv_line,
    invoice,
    load_policy,
    po,
    po_line,
    r_line,
    receipt,
)

FURNITURE = load_policy("furniture/policies/furniture-ap-v2.yaml")
IT = load_policy("it_hardware/policies/it-procurement-v1.yaml")


def run(data: MatchInput, policy=FURNITURE) -> MatchResult:  # type: ignore[no-untyped-def]
    return reconcile(data, policy)


# --- Order 20 chairs, receipt 18, invoice 20 -------------------------------------------


def test_twenty_ordered_eighteen_received_is_quantity_mismatch_with_citations() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "18"),)),),
    )
    result = run(data)
    assert result.outcome == "discrepancies_found"
    [qty] = result.of_type("quantity_mismatch")
    assert qty.reason_code == "invoiced_exceeds_received"
    assert qty.values["invoiced"] == "20"
    assert qty.values["net_received"] == "18"
    assert qty.values["excess"] == "2"
    cited_fields = {ref.field for ref in qty.evidence}
    assert {"po_line.quantity", "invoice_line.quantity", "receipt_line.quantity"} <= cited_fields
    cited_docs = {ref.document_id for ref in qty.evidence}
    assert {"PO-1001", "INV-9001", "GR-5001"} <= cited_docs
    assert not result.of_type("price_mismatch")

    rec = recommend(result, FURNITURE, "CASE-1")
    assert rec.decision == "propose_action"
    assert rec.action_type == "create_follow_up_task"
    assert rec.assignee_group == "receiving"
    # The recommendation is a follow-up, never a resolution.
    assert "resolved" not in rec.title.lower()


# --- No receiving record ---------------------------------------------------------------


def test_missing_receipt_is_needs_information_not_non_delivery() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
        purchase_orders=(po(),),
        receipts=(),
    )
    result = run(data)
    assert result.outcome == "needs_information"
    [missing] = result.of_type("missing_receipt")
    assert missing.status == "needs_information"
    assert "does not establish whether the goods were delivered" in missing.summary
    assert not result.of_type("quantity_mismatch") or all(
        f.reason_code == "invoiced_exceeds_ordered" for f in result.of_type("quantity_mismatch")
    )
    rec = recommend(result, FURNITURE, "CASE-2")
    assert rec.decision == "request_information"
    assert rec.action_type == "create_follow_up_task"


def test_rejected_receipt_lines_are_a_record_but_not_received_quantity() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20", status="rejected"),)),),
    )
    result = run(data)
    assert not result.of_type("missing_receipt")
    [qty] = result.of_type("quantity_mismatch")
    assert qty.values["received_accepted"] == "0"
    assert qty.values["excess"] == "20"


def test_returns_reduce_received_quantity() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
        purchase_orders=(po(),),
        receipts=(
            receipt((r_line("1", "20"),)),
            receipt((r_line("1", "3", kind="return", doc="GR-5002"),), number="GR-5002"),
        ),
    )
    [qty] = run(data).of_type("quantity_mismatch")
    assert qty.values["net_received"] == "17"
    assert qty.values["excess"] == "3"


# --- Price tolerance boundaries --------------------------------------------------------


@pytest.mark.parametrize(
    ("invoice_price", "expect_mismatch"),
    [
        ("185.50", False),  # +0.50 exactly at absolute limit (also < 1% = 1.85)
        ("186.85", False),  # +1.85 exactly at 1% of 185.00
        ("186.86", True),  # one minor unit above the larger (percent) limit
        ("183.15", False),  # -1.85 exactly at 1% below
        ("183.14", True),  # one minor unit beyond, below PO price
    ],
)
def test_price_tolerance_either_mode_boundaries(invoice_price: str, expect_mismatch: bool) -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", invoice_price),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    mismatches = run(data).of_type("price_mismatch")
    assert bool(mismatches) is expect_mismatch


def test_price_tolerance_absolute_only_policy_boundary() -> None:
    v1 = load_policy("furniture/policies/furniture-ap-v1.yaml")  # absolute 1.00 only
    base = dict(purchase_orders=(po(),), receipts=(receipt((r_line("1", "20"),)),))
    at = MatchInput(invoice=invoice((inv_line("1", "1", "20", "186.00"),)), **base)  # type: ignore[arg-type]
    out = MatchInput(invoice=invoice((inv_line("1", "1", "20", "186.01"),)), **base)  # type: ignore[arg-type]
    assert not reconcile(at, v1).of_type("price_mismatch")
    [m] = reconcile(out, v1).of_type("price_mismatch")
    assert m.values["difference"] == "1.0100"


def test_it_policy_both_rules_must_pass_and_over_only() -> None:
    # PO price 1000.00 EUR: absolute 5.00 and 2% (=20.00) must BOTH pass → effective 5.00.
    it_po = po(number="PO-1001", currency="EUR", lines=(po_line("1", "10", "1000.00"),))

    def inv_at(price: str):  # type: ignore[no-untyped-def]
        return MatchInput(
            invoice=invoice((inv_line("1", "1", "10", price),), currency="EUR"),
            purchase_orders=(it_po,),
            receipts=(receipt((r_line("1", "10"),)),),
        )

    assert not reconcile(inv_at("1005.00"), IT).of_type("price_mismatch")
    assert reconcile(inv_at("1005.01"), IT).of_type("price_mismatch")
    # Under-billing is ignored by an over_only policy.
    assert not reconcile(inv_at("900.00"), IT).of_type("price_mismatch")


def test_zero_po_price_with_percent_rule_routes_to_review() -> None:
    zero_po = po(lines=(po_line("1", "20", "0.00"),))
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "10.00"),)),
        purchase_orders=(zero_po,),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    # IT policy needs BOTH rules; percent is undefined for a zero denominator.
    it_data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "10.00"),), currency="EUR"),
        purchase_orders=(po(currency="EUR", lines=(po_line("1", "20", "0.00"),)),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    it_result = reconcile(it_data, IT)
    assert [f.reason_code for f in it_result.findings if f.status == "needs_review"] == [
        "percent_tolerance_zero_price"
    ]
    # Furniture "either": absolute rule is still defined (10.00 > 0.50) → plain mismatch.
    furn = run(data)
    assert furn.of_type("price_mismatch")
    assert not [f for f in furn.findings if f.reason_code == "percent_tolerance_zero_price"]


def test_discount_is_part_of_net_unit_price() -> None:
    # 20 x 190.00 with a 100.00 line discount → net 185.00 → matches PO 185.00.
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "190.00", discount="100.00"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    assert run(data).outcome == "matched"


# --- Prior allocations -----------------------------------------------------------------


def test_receipt_quantity_already_allocated_to_earlier_invoice_is_not_reused() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "10", "185.00"),), number="INV-9002"),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "10"),)),),
        prior_allocations=(allocation("INV-9001", "1", "10"),),
    )
    result = run(data)
    reasons = {f.reason_code for f in result.of_type("quantity_mismatch")}
    assert "receipt_quantity_already_allocated" in reasons
    [f] = [f for f in result.findings if f.reason_code == "receipt_quantity_already_allocated"]
    assert f.values["allocatable"] == "0"
    assert f.values["excess"] == "10"


def test_cancelled_and_same_invoice_allocations_are_ignored() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "10", "185.00"),), number="INV-9002"),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "10"),)),),
        prior_allocations=(
            allocation("INV-9001", "1", "10", status="cancelled"),
            allocation("INV-9002", "1", "10"),  # this invoice's own earlier allocation
        ),
    )
    assert run(data).outcome == "matched"


def test_two_invoice_lines_for_one_po_line_share_receipts() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "10", "185.00"), inv_line("2", "1", "10", "185.00"))),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "15"),)),),
    )
    [qty] = run(data).of_type("quantity_mismatch")
    assert qty.values["invoiced"] == "20"
    assert qty.values["excess"] == "5"
    assert set(qty.invoice_line_numbers) == {"1", "2"}


def test_cumulative_invoicing_beyond_ordered_quantity() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "15", "185.00"),), number="INV-9002"),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "30"),)),),
        prior_allocations=(allocation("INV-9001", "1", "10"),),
    )
    reasons = {f.reason_code for f in run(data).of_type("quantity_mismatch")}
    assert reasons == {"invoiced_exceeds_ordered"}


# --- Units and currencies --------------------------------------------------------------


def test_unknown_unit_routes_to_manual_review_naming_the_unit() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00", unit="crate"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    result = run(data)
    assert result.outcome == "manual_review"
    [f] = [f for f in result.findings if f.status == "needs_review"]
    assert f.reason_code == "unit_unknown"
    assert f.values["condition"] == "crate"


def test_configured_unit_conversion_is_applied_exactly() -> None:
    # Invoice 2 boxes of 10 at 1850.00 per box == 20 EA at 185.00.
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "2", "1850.00", unit="box of 10"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    assert run(data).outcome == "matched"


def test_unconfigured_conversion_is_named() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00", unit="set"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    [f] = [f for f in run(data).findings if f.status == "needs_review"]
    assert f.reason_code == "unit_conversion_unsupported"
    assert f.values["condition"] == "SET->EA"


def test_unsupported_currency_routes_to_review() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),), currency="CHF"),
        purchase_orders=(po(currency="CHF"),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    result = run(data)
    assert result.outcome == "manual_review"
    assert result.findings[0].reason_code == "currency_unsupported"
    assert "CHF" in result.findings[0].summary


def test_currency_mismatch_is_not_converted() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),), currency="EUR"),
        purchase_orders=(po(currency="USD"),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    result = reconcile(data, IT)  # IT supports EUR and USD
    assert result.findings[0].reason_code == "currency_conversion_unsupported"
    assert not result.of_type("price_mismatch")


# --- Identity resolution ---------------------------------------------------------------


def test_similar_supplier_names_do_not_join_silently() -> None:
    data = MatchInput(
        invoice=invoice(
            (inv_line("1", "1", "20", "185.00"),),
            supplier_id=None,
            supplier_name="Acme Office Furnishings LLC",
        ),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "18"),)),),
        suppliers=(
            Supplier("SUP-ACME", "Acme Office Furniture Inc"),
            Supplier("SUP-ACMF", "Acme Office Furnishings LLC (Delaware)"),
        ),
    )
    result = run(data)
    assert result.outcome == "manual_review"
    [f] = result.findings
    assert f.reason_code == "supplier_match_uncertain"
    assert "SUP-ACME" in f.values["candidate_supplier_ids"]
    # No line-level arithmetic happens on an unconfirmed supplier join.
    assert not result.of_type("quantity_mismatch")


def test_supplier_id_mismatch_is_review() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),), supplier_id="SUP-OTHER"),
        purchase_orders=(po(),),
    )
    assert run(data).findings[0].reason_code == "supplier_mismatch"


def test_missing_po_record_is_needs_information() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),), po_number="PO-404"),
        purchase_orders=(po(),),
    )
    result = run(data)
    assert result.outcome == "needs_information"
    assert result.findings[0].reason_code == "po_record_unavailable"


def test_line_matched_by_unique_item_code_is_disclosed() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", None, "20", "185.00", item="ITEM-1"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    result = run(data)
    assert result.outcome == "matched"
    assert result.findings[0].reason_code == "po_line_matched_by_item_code"


# --- Conflicting sources ---------------------------------------------------------------


def test_supplier_email_conflict_resolved_by_defined_precedence() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "18"),)),),
        supplier_assertions=(assertion("20"),),
    )
    result = run(data)
    [conflict] = result.of_type("source_conflict")
    assert conflict.status == "informational"
    assert conflict.values["controlling_source"] == "goods_receipt"
    assert result.of_type("quantity_mismatch")  # receipt (18) still drives the arithmetic
    assert result.outcome == "discrepancies_found"


def test_supplier_email_conflict_escalates_when_precedence_undefined() -> None:
    it_po = po(currency="EUR")
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),), currency="EUR"),
        purchase_orders=(it_po,),
        receipts=(receipt((r_line("1", "18"),)),),
        supplier_assertions=(assertion("20"),),
    )
    result = reconcile(data, IT)
    [conflict] = result.of_type("source_conflict")
    assert conflict.status == "needs_review"
    assert conflict.reason_code == "source_precedence_undefined"
    assert result.outcome == "manual_review"
    # The quantity discrepancy is still reported independently.
    assert result.of_type("quantity_mismatch")


def test_supplier_claim_without_receipt_does_not_establish_delivery() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
        purchase_orders=(po(),),
        supplier_assertions=(assertion("20"),),
    )
    result = run(data)
    assert result.outcome == "needs_information"
    assert {f.reason_code for f in result.findings} >= {
        "receiving_record_unavailable",
        "supplier_claim_unverified",
    }


# --- Multiple independent discrepancies -----------------------------------------------


def test_quantity_and_price_discrepancies_reported_independently() -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", "20", "185.00"), inv_line("2", "2", "5", "99.00"))),
        purchase_orders=(po(lines=(po_line("1", "20", "185.00"), po_line("2", "5", "90.00"))),),
        receipts=(receipt((r_line("1", "18"), r_line("2", "5"))),),
    )
    result = run(data)
    assert [f.po_line_number for f in result.of_type("quantity_mismatch")] == ["1"]
    assert [f.po_line_number for f in result.of_type("price_mismatch")] == ["2"]
    rec = recommend(result, FURNITURE, "CASE-X")
    assert rec.route == "quantity_mismatch"  # documented route priority


def test_charge_lines_are_excluded_and_disclosed() -> None:
    data = MatchInput(
        invoice=invoice(
            (inv_line("1", "1", "20", "185.00"), inv_line("2", None, "1", "45.00", kind="charge"))
        ),
        purchase_orders=(po(),),
        receipts=(receipt((r_line("1", "20"),)),),
    )
    result = run(data)
    assert result.outcome == "matched"
    assert result.findings[0].reason_code == "charges_not_evaluated"


# --- Property: quantity rule equals the hand-written oracle ----------------------------


@given(
    ordered=st.integers(min_value=1, max_value=500),
    received=st.integers(min_value=0, max_value=500),
    prior=st.integers(min_value=0, max_value=500),
    invoiced=st.integers(min_value=1, max_value=500),
)
def test_quantity_rule_matches_oracle(
    ordered: int, received: int, prior: int, invoiced: int
) -> None:
    data = MatchInput(
        invoice=invoice((inv_line("1", "1", str(invoiced), "185.00"),), number="INV-NEW"),
        purchase_orders=(po(lines=(po_line("1", str(ordered), "185.00"),)),),
        receipts=(receipt((r_line("1", str(received)),)),),
        prior_allocations=(allocation("INV-OLD", "1", str(prior)),) if prior else (),
    )
    result = run(data)
    over_received = invoiced > received - prior
    over_ordered = prior + invoiced > ordered
    reasons = {f.reason_code for f in result.of_type("quantity_mismatch")}
    assert ("invoiced_exceeds_ordered" in reasons) is over_ordered
    assert bool(reasons - {"invoiced_exceeds_ordered"}) is over_received
    if over_received:
        [f] = [
            f
            for f in result.of_type("quantity_mismatch")
            if f.reason_code != "invoiced_exceeds_ordered"
        ]
        assert Decimal(f.values["excess"]) == invoiced - (received - prior)


# --- Negative quantities and prices ------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "named"),
    [
        (
            MatchInput(
                invoice=invoice((inv_line("1", "1", "-20", "185.00"),)),
                purchase_orders=(po(),),
                receipts=(receipt((r_line("1", "18"),)),),
            ),
            "invoice line 1 quantity -20",
        ),
        (
            MatchInput(
                invoice=invoice((inv_line("1", "1", "20", "-185.00"),)),
                purchase_orders=(po(),),
                receipts=(receipt((r_line("1", "20"),)),),
            ),
            "invoice line 1 unit price -185.00",
        ),
        (
            MatchInput(
                invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
                purchase_orders=(po(lines=(po_line("1", "-20", "185.00"),)),),
                receipts=(receipt((r_line("1", "20"),)),),
            ),
            "PO-1001 line 1 quantity -20",
        ),
        (
            MatchInput(
                invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
                purchase_orders=(po(),),
                receipts=(receipt((r_line("1", "-18"),)),),
            ),
            "GR-5001 line 1 quantity -18",
        ),
        (
            MatchInput(
                invoice=invoice((inv_line("1", "1", "20", "185.00"),)),
                purchase_orders=(po(),),
                receipts=(receipt((r_line("1", "20"),)),),
                prior_allocations=(allocation("INV-8000", "1", "-5"),),
            ),
            "allocation of INV-8000 to PO line 1 quantity -5",
        ),
    ],
    ids=["invoice quantity", "invoice price", "po quantity", "receipt quantity", "allocation"],
)
def test_a_negative_quantity_or_price_goes_to_review_not_to_a_match(
    data: MatchInput, named: str
) -> None:
    """Credit notes and negative values are not modelled. An invoice quantity of -20 used to
    count as fully covered by the 18 received, so the case matched and could be resolved."""
    result = run(data)
    assert result.outcome == "manual_review", [f.reason_code for f in result.findings]
    [finding] = result.findings
    assert finding.reason_code == "negative_value" and named.lower() in finding.summary.lower()
