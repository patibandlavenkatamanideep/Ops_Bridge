"""Receiving and disposition facts: what was delivered, accepted, rejected and remedied.

Expected results are written from the scenario, by hand (docs/receiving-disposition.md):
every case bills 20 EA on PO-1001 line 1, ordered 20 at 185.00. Policy furniture-ap@v2 names
no owner for rejected goods; `CONFIGURED` is the same policy with a disposition owner
(procurement) and a hold scope, as a later policy version would publish it.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest

from opsbridge.domain.decision import recommend
from opsbridge.domain.matching import RULES_VERSION, MatchResult, reconcile
from opsbridge.domain.policy import ActionRoute, ActionType, ReconciliationPolicy
from opsbridge.domain.receiving import positions
from opsbridge.domain.records import MatchInput, ReceiptLine, ReceivingEvent

from .builders import (
    D,
    allocation,
    event,
    inv_line,
    invoice,
    load_policy,
    po,
    po_line,
    r_line,
    receipt,
)

V2 = load_policy("furniture/policies/furniture-ap-v2.yaml")
DISPOSITION = ActionRoute(
    action_type="create_follow_up_task",
    assignee_group="procurement",
    title_template="Resolve rejected units with the supplier for {po} ({case})",
)


def configured(hold: str | None = "disputed_portion") -> ReconciliationPolicy:
    routes = {**V2.action_routes, "disposition": DISPOSITION}
    return V2.model_copy(update={"action_routes": routes, "disputed_quantity_hold": hold})


CONFIGURED = configured()
INVOICE = invoice((inv_line("1", "1", "20", "185.00"),))
ORDER = po(lines=(po_line("1", "20", "185.00"),))
DAMAGED = "damaged in transit"


def case(
    *receipt_lines: ReceiptLine,
    events: tuple[ReceivingEvent, ...] = (),
    inv: Any = INVOICE,
    order: Any = ORDER,
    allocations: tuple[Any, ...] = (),
) -> MatchInput:
    receipts = (receipt(receipt_lines),) if receipt_lines else ()
    return MatchInput(
        inv, (order,), receipts, prior_allocations=allocations, receiving_events=events
    )


def short_delivery() -> MatchInput:
    """A: ordered 20; 18 arrive and receiving accepts the 18."""
    return case(r_line("1", "18"))


def rejected_at_receiving(reason: str | None = DAMAGED, **kw: Any) -> MatchInput:
    """B: ordered 20; 20 arrive; receiving accepts 18 and rejects 2 as damaged."""
    return case(r_line("1", "18"), r_line("1", "2", status="rejected", row=3, reason=reason), **kw)


def codes(result: MatchResult) -> list[str]:
    return sorted(f"{f.finding_type}/{f.reason_code}" for f in result.findings)


def only(result: MatchResult, reason_code: str) -> Any:
    found = [f for f in result.findings if f.reason_code == reason_code]
    assert len(found) == 1, codes(result)
    return found[0]


def cited(finding: Any) -> list[str]:
    return [ref.field + "@" + ref.locator for ref in finding.evidence]


# --- 1 and 2: the two 18-unit cases ------------------------------------------------------


def test_a_short_delivery_asks_receiving_about_the_two_undelivered_units() -> None:
    result = reconcile(short_delivery(), CONFIGURED)
    assert result.rules_version == RULES_VERSION == "opsbridge-rules/1.4.0"
    assert result.outcome == "discrepancies_found"
    assert codes(result) == [
        "note/hold_disputed_portion",
        "quantity_mismatch/invoiced_exceeds_received",
    ]
    f = only(result, "invoiced_exceeds_received")
    assert f.values["delivered"] == "18"
    assert f.values["received_accepted"] == "18"
    assert f.values["undelivered"] == "2"
    assert "received_rejected" not in f.values
    assert "18 were delivered and accepted" in f.summary
    assert "2 EA have not been delivered" in f.summary
    rec = recommend(result, CONFIGURED, "CASE-1")
    assert (rec.label, rec.assignee_group) == ("propose_action:create_follow_up_task", "receiving")
    assert rec.title == "Confirm delivered quantity for PO-1001 (CASE-1)"
    (p,) = positions(short_delivery(), CONFIGURED)
    assert (p.ordered, p.delivered, p.accepted, p.rejected, p.undelivered) == (
        D("20"),
        D("18"),
        D("18"),
        D("0"),
        D("2"),
    )


def test_b_two_rejected_units_ask_procurement_for_their_disposition_not_delivery() -> None:
    result = reconcile(rejected_at_receiving(), CONFIGURED)
    assert result.outcome == "discrepancies_found"
    assert codes(result) == ["disposition/disposition_unrecorded", "note/hold_disputed_portion"]
    f = only(result, "disposition_unrecorded")
    assert f.status == "discrepancy"
    assert f.values["delivered"] == "20"
    assert f.values["received_accepted"] == "18"
    assert f.values["received_rejected"] == "2"
    assert f.values["undelivered"] == "0"
    assert f.values["rejection_reasons"] == DAMAGED
    assert "2 EA delivered on PO line 1 were rejected at receiving" in f.summary
    assert DAMAGED in f.summary
    assert "no return, credit or replacement is recorded" in f.summary
    # The rejected row's quantity, status and reason are cited, as well as the accepted row.
    assert "receipt_line.quantity@csv:row=3;col=qty_accepted;line=1" in cited(f)
    assert "receipt_line.status@csv:row=3;col=status" in cited(f)
    assert "receipt_line.reason@csv:row=3;col=reason" in cited(f)
    rec = recommend(result, CONFIGURED, "CASE-1")
    assert (rec.label, rec.assignee_group) == (
        "propose_action:create_follow_up_task",
        "procurement",
    )
    assert rec.title == "Resolve rejected units with the supplier for PO-1001 (CASE-1)"
    (p,) = positions(rejected_at_receiving(), CONFIGURED)
    assert (p.ordered, p.delivered, p.accepted, p.rejected, p.undelivered) == (
        D("20"),
        D("20"),
        D("18"),
        D("2"),
        D("0"),
    )
    assert p.disposition == "unrecorded"


def test_the_two_18_unit_cases_differ_in_facts_citations_and_next_question() -> None:
    a = reconcile(short_delivery(), CONFIGURED)
    b = reconcile(rejected_at_receiving(), CONFIGURED)
    fa, fb = only(a, "invoiced_exceeds_received"), only(b, "disposition_unrecorded")
    assert fa.values["received_accepted"] == fb.values["received_accepted"] == "18"
    assert (fa.values["delivered"], fb.values["delivered"]) == ("18", "20")
    assert (fa.values["undelivered"], fb.values["undelivered"]) == ("2", "0")
    assert set(cited(fb)) - set(cited(fa))  # B cites the rejected row; A has no such row
    ra, rb = recommend(a, CONFIGURED, "C"), recommend(b, CONFIGURED, "C")
    assert (ra.route, ra.assignee_group) == ("quantity_mismatch", "receiving")
    assert (rb.route, rb.assignee_group) == ("disposition", "procurement")


def test_a_rejection_without_a_recorded_reason_asks_receiving_for_the_inspection() -> None:
    result = reconcile(rejected_at_receiving(reason=None), CONFIGURED)
    assert result.outcome == "needs_information"
    f = only(result, "rejection_reason_missing")
    assert (f.finding_type, f.status) == ("disposition", "needs_information")
    assert "no reason is recorded" in f.summary
    rec = recommend(result, CONFIGURED, "C")
    assert (rec.label, rec.assignee_group) == (
        "request_information:create_follow_up_task",
        "receiving",
    )


def test_with_no_disposition_owner_configured_it_says_so_and_picks_no_recipient() -> None:
    result = reconcile(rejected_at_receiving(), V2)
    assert result.outcome == "manual_review"
    f = only(result, "disposition_owner_unconfigured")
    assert f.status == "needs_review"
    assert "furniture-ap@v2 names no owner for rejected goods" in f.summary
    assert "does not choose whom to ask" in f.summary
    assert only(result, "disposition_unrecorded").values["received_rejected"] == "2"
    rec = recommend(result, V2, "C")
    assert (rec.label, rec.assignee_group) == ("escalate:assign_case", "ap-review")


def test_rules_1_3_0_replays_the_old_reading_of_a_rejection() -> None:
    result = reconcile(rejected_at_receiving(), CONFIGURED, "opsbridge-rules/1.3.0")
    assert codes(result) == ["quantity_mismatch/invoiced_exceeds_received"]
    assert recommend(result, CONFIGURED, "C").assignee_group == "receiving"


# --- 3: returned, credit promised, then credited -------------------------------------------


def returned(*more: ReceivingEvent) -> MatchInput:
    rtv = event("RTV-1", "return", "2", receipt_number="GR-5001", occurred="2026-09-24", row=2)
    return rejected_at_receiving(events=(rtv, *more))


def test_a_return_does_not_subtract_the_rejected_units_again() -> None:
    result = reconcile(returned(), CONFIGURED)
    assert not result.of_type("quantity_mismatch")
    f = only(result, "return_credit_unrecorded")
    assert f.values["received_accepted"] == "18"
    assert f.values["returned"] == "2"
    assert "returned" in f.summary and "no credit is recorded" in f.summary
    (p,) = positions(returned(), CONFIGURED)
    assert (p.accepted, p.rejected, p.returned) == (D("18"), D("2"), D("2"))


def test_a_promised_credit_is_pending_until_a_credit_document_is_posted() -> None:
    promise = event("CP-1", "credit_promised", "2", follows="RTV-1", row=3)
    pending = reconcile(returned(promise), CONFIGURED)
    f = only(pending, "credit_pending")
    assert (f.finding_type, f.status) == ("disposition", "discrepancy")
    assert f.values["credit_promised"] == "2"
    assert f.values["credit_posted"] == "0"
    assert "a promise is not a credit" in f.summary
    assert recommend(pending, CONFIGURED, "C").assignee_group == "procurement"

    credit = event(
        "CN-1",
        "credit_posted",
        "2",
        follows="RTV-1",
        invoice_number="INV-9001",
        document_number="CN-77",
        row=4,
    )
    resolved = reconcile(returned(promise, credit), CONFIGURED)
    assert resolved.outcome == "matched"
    note = only(resolved, "rejected_units_credited")
    assert note.status == "informational"
    assert "CN-77" in note.summary
    assert "receiving_event.document_number@csv:row=4;col=document" in cited(note)
    (p,) = positions(returned(promise, credit), CONFIGURED)
    assert (p.rejected, p.returned, p.credit_posted, p.disposition) == (
        D("2"),
        D("2"),
        D("2"),
        "credited",
    )


def test_a_credit_for_another_invoice_does_not_resolve_this_one() -> None:
    elsewhere = event(
        "CN-1", "credit_posted", "2", follows="RTV-1", invoice_number="INV-7777", row=4
    )
    result = reconcile(returned(elsewhere), CONFIGURED)
    assert result.outcome != "matched"
    f = only(result, "return_credit_unrecorded")
    assert "INV-7777" in f.summary


# --- 4: replacement received, then accepted --------------------------------------------------


def replaced(*more: ReceivingEvent) -> MatchInput:
    rd = event("RD-1", "replacement_delivery", "2", receipt_number="GR-5001", row=2)
    return rejected_at_receiving(events=(rd, *more))


def test_a_replacement_not_yet_accepted_leaves_acceptance_to_receiving() -> None:
    result = reconcile(replaced(), CONFIGURED)
    assert result.outcome == "needs_information"
    f = only(result, "replacement_acceptance_unrecorded")
    assert (f.finding_type, f.status) == ("disposition", "needs_information")
    assert f.values["replacement_delivered"] == "2"
    assert f.values["replacement_accepted"] == "0"
    rec = recommend(result, CONFIGURED, "C")
    assert (rec.decision, rec.assignee_group) == ("request_information", "receiving")


def test_an_accepted_replacement_covers_the_line_without_double_counting() -> None:
    ra = event("RA-1", "replacement_acceptance", "2", follows="RD-1", row=3)
    result = reconcile(replaced(ra), CONFIGURED)
    assert result.outcome == "matched"
    only(result, "rejected_units_replaced")
    (p,) = positions(replaced(ra), CONFIGURED)
    # The order is unchanged and the rejection stays in the history.
    assert p.ordered == D("20")
    assert (p.delivered, p.accepted, p.rejected) == (D("20"), D("18"), D("2"))
    assert (p.replacement_delivered, p.replacement_accepted) == (D("2"), D("2"))
    assert p.covered == D("20")
    assert p.disposition == "replaced"
    assert [r.quantity for r in p.rejections] == [D("2")]


def test_a_promised_replacement_that_has_not_arrived_is_pending() -> None:
    rp = event("RP-1", "replacement_promised", "2", receipt_number="GR-5001", row=2)
    result = reconcile(rejected_at_receiving(events=(rp,)), CONFIGURED)
    f = only(result, "replacement_pending")
    assert f.status == "discrepancy"
    assert recommend(result, CONFIGURED, "C").assignee_group == "procurement"


# --- 5: a second charge for the replacement -----------------------------------------------


def test_a_later_invoice_for_the_replaced_units_is_flagged_for_ap_review() -> None:
    ra = event("RA-1", "replacement_acceptance", "2", follows="RD-1", row=3)
    second = invoice((inv_line("1", "1", "2", "185.00"),), number="INV-9002")
    data = replaced(ra)
    data = MatchInput(
        second,
        data.purchase_orders,
        data.receipts,
        prior_allocations=(allocation("INV-9001", "1", "20"),),
        receiving_events=data.receiving_events,
    )
    result = reconcile(data, CONFIGURED)
    f = only(result, "possible_duplicate_replacement_charge")
    assert f.status == "needs_review"
    assert "RD-1" in f.summary and "INV-9001" in f.summary
    assert "receiving_event.quantity@csv:row=2;col=qty" in cited(f)
    assert recommend(result, CONFIGURED, "C").label == "escalate:assign_case"


def test_a_new_order_for_the_same_item_and_quantity_is_not_flagged() -> None:
    order = po(number="PO-1002", lines=(po_line("1", "2", "185.00"),))
    inv = invoice((inv_line("1", "1", "2", "185.00"),), number="INV-9003", po_number="PO-1002")
    data = MatchInput(
        inv,
        (order,),
        (receipt((r_line("1", "2"),), number="GR-5002", po_number="PO-1002"),),
    )
    result = reconcile(data, CONFIGURED)
    assert result.outcome == "matched"
    assert not [f for f in result.findings if "duplicate" in f.reason_code]


def test_billing_past_the_order_without_a_replacement_is_not_called_a_duplicate() -> None:
    second = invoice((inv_line("1", "1", "2", "185.00"),), number="INV-9002")
    data = case(r_line("1", "20"), inv=second, allocations=(allocation("INV-9001", "1", "20"),))
    result = reconcile(data, CONFIGURED)
    assert "quantity_mismatch/invoiced_exceeds_ordered" in codes(result)
    assert not [f for f in result.findings if "duplicate" in f.reason_code]


# --- 6 (rules part): a rejection learned after the receipt was posted ----------------------


def late_rejection(qty: str = "2", receipt_number: str = "GR-5001") -> MatchInput:
    rej = event(
        "REJ-1",
        "rejection",
        qty,
        receipt_number=receipt_number,
        reason=DAMAGED,
        occurred="2026-09-20",
        row=2,
    )
    return case(r_line("1", "20"), events=(rej,))


def test_a_late_rejection_moves_units_from_accepted_to_rejected() -> None:
    result = reconcile(late_rejection(), CONFIGURED)
    f = only(result, "disposition_unrecorded")
    assert f.values["delivered"] == "20"
    assert f.values["received_accepted"] == "18"
    assert f.values["received_rejected"] == "2"
    assert "receiving_event.quantity@csv:row=2;col=qty" in cited(f)
    (p,) = positions(late_rejection(), CONFIGURED)
    (rej,) = p.rejections
    assert (rej.source, rej.late, rej.occurred, rej.reason) == (
        "REJ-1",
        True,
        "2026-09-20",
        DAMAGED,
    )


def test_a_late_rejection_of_more_than_its_receipt_accepted_is_not_applied() -> None:
    result = reconcile(late_rejection(qty="25"), CONFIGURED)
    assert result.outcome == "manual_review"
    f = only(result, "event_quantity_exceeds_reference")
    assert "REJ-1" in f.summary and "25" in f.summary


# --- 8 (rules part): references that cannot be resolved, and unstated acceptance ---------


def test_an_event_whose_receipt_has_not_arrived_waits_and_is_never_matched() -> None:
    result = reconcile(late_rejection(receipt_number="GR-5009"), CONFIGURED)
    assert result.outcome == "needs_information"
    f = only(result, "event_reference_unresolved")
    assert (f.finding_type, f.status) == ("missing_receipt", "needs_information")
    assert "GR-5009" in f.summary and "not applied" in f.summary


def test_an_event_following_an_unknown_event_waits() -> None:
    ra = event("RA-1", "replacement_acceptance", "2", follows="RD-404")
    result = reconcile(rejected_at_receiving(events=(ra,)), CONFIGURED)
    assert only(result, "event_reference_unresolved").status == "needs_information"


def test_an_event_for_a_line_the_order_does_not_have_is_not_attached_elsewhere() -> None:
    rej = event("REJ-1", "rejection", "2", po_line_number="9", receipt_number="GR-5001")
    result = reconcile(case(r_line("1", "20"), events=(rej,)), CONFIGURED)
    assert result.outcome == "manual_review"
    assert "line 9" in only(result, "event_line_unknown").summary


@pytest.mark.parametrize(
    ("change", "reason_code"),
    [
        ({"unit": "crate"}, "unit_unknown"),
        ({"quantity": None}, "event_quantity_missing"),
        ({"event_type": "replacement_acceptance", "follows": "REJ-0"}, "event_reference_mismatch"),
    ],
)
def test_events_that_cannot_be_read_exactly_go_to_review(
    change: dict[str, Any], reason_code: str
) -> None:
    rej0 = event("REJ-0", "rejection", "2", receipt_number="GR-5001", reason=DAMAGED)
    base = event("RTV-1", "return", "2", follows="REJ-0", row=3)
    bad = ReceivingEvent(**{**base.__dict__, **change})
    result = reconcile(case(r_line("1", "20"), events=(rej0, bad)), CONFIGURED)
    assert result.outcome == "manual_review"
    assert reason_code in [f.reason_code for f in result.findings]


def test_a_negative_event_quantity_is_refused_like_any_negative_quantity() -> None:
    result = reconcile(late_rejection(qty="-2"), CONFIGURED)
    assert result.outcome == "manual_review"
    assert "REJ-1" in only(result, "negative_value").summary


def test_a_received_quantity_without_an_acceptance_status_is_never_counted_accepted() -> None:
    data = case(r_line("1", "20", status="unstated"))
    result = reconcile(data, CONFIGURED)
    assert result.outcome == "needs_information"
    f = only(result, "acceptance_not_stated")
    assert (f.finding_type, f.status) == ("missing_receipt", "needs_information")
    assert "does not say whether they were accepted" in f.summary
    (p,) = positions(data, CONFIGURED)
    assert (p.delivered, p.accepted) == (D("20"), None)


# --- 9: the split under each hold policy; nothing pays ------------------------------------


@pytest.mark.parametrize(
    ("hold", "reason_code", "phrase"),
    [
        ("whole_invoice", "hold_whole_invoice", "holds the whole invoice"),
        ("disputed_portion", "hold_disputed_portion", "only the disputed portion"),
        (None, "hold_unconfigured", "the payment recommendation is left unresolved"),
    ],
)
def test_the_split_is_shown_under_each_hold_policy_and_nothing_is_paid(
    hold: str | None, reason_code: str, phrase: str
) -> None:
    policy = configured(hold)
    result = reconcile(rejected_at_receiving(), policy)
    note = only(result, reason_code)
    assert note.status == "informational"
    assert (note.values["matched_quantity"], note.values["disputed_quantity"]) == ("18", "2")
    assert phrase in note.summary
    assert "does not release, short-pay or schedule payment" in note.summary
    assert "amount" not in note.values  # no amount from quantity alone
    rec = recommend(result, policy, "C")
    assert rec.action_type in get_args(ActionType)


def test_no_action_type_can_pay_short_pay_or_post_a_credit() -> None:
    assert set(get_args(ActionType)) == {
        "create_follow_up_task",
        "assign_case",
        "create_supplier_message_draft",
        "record_review_note",
    }


def test_existing_policy_documents_are_unchanged_by_the_new_fields() -> None:
    document = V2.model_dump(mode="json")
    assert "disputed_quantity_hold" not in document
    assert "disposition" not in document["action_routes"]
    assert configured("whole_invoice").model_dump(mode="json")["disputed_quantity_hold"] == (
        "whole_invoice"
    )


# --- the matcher's own wording passes the checks model text is held to ----------------------


def _all_inputs() -> list[tuple[str, MatchInput]]:
    promise = event("CP-1", "credit_promised", "2", follows="RTV-1", row=3)
    credit = event(
        "CN-1", "credit_posted", "2", follows="RTV-1", invoice_number="INV-9001",
        document_number="CN-77", row=4,
    )  # fmt: skip
    elsewhere = event(
        "CN-1", "credit_posted", "2", follows="RTV-1", invoice_number="INV-7777", row=4
    )
    ra = event("RA-1", "replacement_acceptance", "2", follows="RD-1", row=3)
    rp = event("RP-1", "replacement_promised", "2", receipt_number="GR-5001", row=2)
    second = invoice((inv_line("1", "1", "2", "185.00"),), number="INV-9002")
    dup = replaced(ra)
    return [
        ("short", short_delivery()),
        ("rejected", rejected_at_receiving()),
        ("no-reason", rejected_at_receiving(reason=None)),
        ("returned", returned()),
        ("promised", returned(promise)),
        ("credited", returned(promise, credit)),
        ("elsewhere", returned(elsewhere)),
        ("replacement", replaced()),
        ("replaced", replaced(ra)),
        ("replacement-promised", rejected_at_receiving(events=(rp,))),
        (
            "second-charge",
            MatchInput(
                second,
                dup.purchase_orders,
                dup.receipts,
                prior_allocations=(allocation("INV-9001", "1", "20"),),
                receiving_events=dup.receiving_events,
            ),
        ),
        ("late", late_rejection()),
        ("late-too-many", late_rejection(qty="25")),
        ("out-of-order", late_rejection(receipt_number="GR-5009")),
        ("unstated", case(r_line("1", "20", status="unstated"))),
    ]


@pytest.mark.parametrize(
    ("name", "data"), _all_inputs(), ids=lambda x: x if isinstance(x, str) else ""
)
@pytest.mark.parametrize("policy", [V2, CONFIGURED, configured("whole_invoice"), configured(None)])
def test_receiving_summaries_state_only_recorded_numbers(
    name: str, data: MatchInput, policy: ReconciliationPolicy
) -> None:
    from opsbridge.agents.steps import (
        AgentContext,
        _allowed_numbers,
        _comparison_problems,
        _date_problems,
        _numbers,
        _role_problems,
    )

    result = reconcile(data, policy)
    for f in result.findings:
        d = f.to_dict()
        ctx = AgentContext(
            case={},
            recon={"findings": [d]},
            policy_out={"policy_ref": result.policy_ref},
            policy=policy,
        )
        unsupported = _numbers(d["summary"]) - _allowed_numbers(ctx, [])
        assert not unsupported, f"{d['reason_code']}: {d['summary']} states {sorted(unsupported)}"
        assert _role_problems(ctx, d["summary"]) == [], d["summary"]
        assert _comparison_problems(ctx, d["summary"]) == [], d["summary"]
        assert _date_problems(ctx, d["summary"], []) == [], d["summary"]
