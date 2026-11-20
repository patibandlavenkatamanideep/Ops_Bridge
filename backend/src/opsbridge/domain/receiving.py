"""The receiving position of a purchase order line, derived from receipts and events.

Pure functions. Quantities are in the PO line's canonical unit, converted only by the
policy's declared conversions (units.py). How each total is derived
(docs/receiving-disposition.md):

- delivered: units on receipt lines of kind `receipt` whose posting stands (accepted,
  rejected or unstated); a reversed posting is not a delivery. None without a receipt.
- accepted: accepted receipt lines, less the units a later `rejection` event moves to
  rejected. None when any delivered units have an unstated acceptance: a generic "received"
  quantity is never read as accepted.
- rejected: rejected receipt lines plus rejection events. Each rejected unit is counted once:
  units rejected at receiving were never accepted, so returning them subtracts nothing more.
- returned_after_acceptance: RETURN movements on receipts (accepted, then sent back).
- covered: accepted, less returns after acceptance, plus accepted replacements: what the line
  can be billed for. A replacement never changes the ordered quantity, and the rejection it
  replaces stays in the history.
- Remedies of rejected units are tracked apart from movement: a returned unit is not a
  credited one, and a promise (of credit or replacement) resolves nothing. A posted credit
  resolves units only for the invoice it names.

An event is applied only when what it refers to is in the input: its receipt, or the event it
follows, on the same PO line. Otherwise it is reported (`problems`) and not applied, so
nothing is concluded from the line until the missing record arrives.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from .money import decimal_str
from .policy import ReconciliationPolicy
from .records import (
    GoodsReceipt,
    MatchInput,
    POLine,
    PurchaseOrder,
    ReceivingEvent,
    SourceRef,
)
from .units import canonical_unit, conversion_factor
from .validation import norm_id

ZERO = Decimal(0)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Which events each kind of event may follow, and which need a quantity.
_PARENTS: dict[str, tuple[str, ...]] = {
    "return": ("rejection",),
    "replacement_promised": ("rejection",),
    "replacement_delivery": ("rejection", "replacement_promised"),
    "replacement_acceptance": ("replacement_delivery",),
    "credit_promised": ("rejection", "return"),
    "credit_posted": ("rejection", "return", "credit_promised"),
}
_QUANTIFIED = (
    "rejection",
    "return",
    "replacement_delivery",
    "replacement_acceptance",
    "credit_posted",
)
_EVENT_FIELDS = (
    "receiving_event.event_type",
    "receiving_event.quantity",
    "receiving_event.unit",
    "receiving_event.occurred",
    "receiving_event.receipt_number",
    "receiving_event.follows",
    "receiving_event.invoice_number",
    "receiving_event.document_number",
    "receiving_event.reason",
)

Disposition = Literal[
    "none",  # nothing was rejected
    "unrecorded",  # rejected, and no return, credit or replacement is recorded
    "returned",  # sent back; no credit recorded
    "credit_pending",  # a credit was promised, none posted for this invoice
    "replacement_pending",  # a replacement was promised and has not arrived
    "replacement_unaccepted",  # a replacement arrived; its acceptance is not recorded
    "credited",  # covered by a posted credit for this invoice
    "replaced",  # covered by accepted replacements
    "resolved",  # covered by credits and replacements together
]


def precision(occurred: str | None) -> Literal["date", "time", "unknown"]:
    """How exactly the source states when an event occurred."""
    if occurred is None:
        return "unknown"
    return "date" if _DATE.match(occurred) else "time"


def event_refs(ev: ReceivingEvent) -> tuple[SourceRef, ...]:
    return tuple(ev.refs[k] for k in _EVENT_FIELDS if k in ev.refs)


@dataclass(frozen=True)
class Rejection:
    """Units refused at inspection: on the receipt itself, or by a later event."""

    source: str  # the receipt number, or the rejection event's id
    receipt_number: str | None
    quantity: Decimal
    reason: str | None
    occurred: str | None  # the receipt's date, or the event's occurrence as stated
    late: bool  # recorded as its own event, after the receipt
    evidence: tuple[SourceRef, ...]


@dataclass(frozen=True)
class EventProblem:
    """Why an event was not applied: the record it refers to is missing (needs information),
    or it cannot be read exactly (needs review)."""

    status: Literal["needs_information", "needs_review"]
    code: str
    detail: str
    evidence: tuple[SourceRef, ...] = ()
    values: dict[str, str] = field(default_factory=dict)  # every number `detail` states


@dataclass(frozen=True)
class LinePosition:
    po_number: str
    po_line_number: str
    unit: str
    ordered: Decimal
    delivered: Decimal | None
    accepted: Decimal | None
    rejected: Decimal
    returned_after_acceptance: Decimal
    returned: Decimal
    replacement_promised: Decimal
    replacement_delivered: Decimal
    replacement_accepted: Decimal
    credit_promised: Decimal
    credit_posted: Decimal  # posted for the invoice being matched
    credits_elsewhere: tuple[str, ...]  # posted credits that name another invoice, or none
    rejections: tuple[Rejection, ...]
    applied: tuple[ReceivingEvent, ...]
    problems: tuple[EventProblem, ...]
    receipt_lines: int  # receipt lines read for this PO line (0: no receiving record)
    accepted_evidence: tuple[SourceRef, ...] = field(default=())
    remedy_evidence: tuple[SourceRef, ...] = field(default=())
    promised_credit_unquantified: bool = False
    promised_replacement_unquantified: bool = False

    @property
    def undelivered(self) -> Decimal | None:
        if self.delivered is None:
            return None
        return max(self.ordered - self.delivered, ZERO)

    @property
    def covered(self) -> Decimal | None:
        if self.accepted is None:
            return None
        return self.accepted - self.returned_after_acceptance + self.replacement_accepted

    @property
    def open_rejected(self) -> Decimal:
        """Rejected units no posted credit (for this invoice) or accepted replacement covers."""
        return max(self.rejected - self.credit_posted - self.replacement_accepted, ZERO)

    @property
    def reasons(self) -> list[str]:
        return sorted({r.reason for r in self.rejections if r.reason})

    @property
    def disposition(self) -> Disposition:
        if self.rejected == 0:
            return "none"
        if self.open_rejected == 0:
            if self.credit_posted and self.replacement_accepted:
                return "resolved"
            return "credited" if self.credit_posted else "replaced"
        if self.replacement_delivered > self.replacement_accepted:
            return "replacement_unaccepted"
        if self.credit_promised or self.promised_credit_unquantified:
            return "credit_pending"
        if self.returned:
            return "returned"
        if self.replacement_promised or self.promised_replacement_unquantified:
            return "replacement_pending"
        return "unrecorded"

    def values(self) -> dict[str, str]:
        """The position as finding values: every number a summary states."""
        out = {"ordered": decimal_str(self.ordered), "unit": self.unit}
        if self.delivered is not None:
            out["delivered"] = decimal_str(self.delivered)
            out["undelivered"] = decimal_str(self.undelivered or ZERO)
        if self.accepted is not None:
            out["received_accepted"] = decimal_str(self.accepted)
        if self.rejected or self.applied:
            out.update(
                {
                    "received_rejected": decimal_str(self.rejected),
                    "returned": decimal_str(self.returned),
                    "replacement_delivered": decimal_str(self.replacement_delivered),
                    "replacement_accepted": decimal_str(self.replacement_accepted),
                    "credit_promised": decimal_str(self.credit_promised),
                    "credit_posted": decimal_str(self.credit_posted),
                    "rejection_reasons": "; ".join(self.reasons),
                    "disposition": self.disposition,
                }
            )
        return out


def _convert(
    qty: Decimal, raw_unit: str | None, po_unit: str, policy: ReconciliationPolicy
) -> Decimal | None:
    canonical = canonical_unit(raw_unit, policy.unit_aliases)
    if canonical is None:
        return None
    factor = conversion_factor(canonical, po_unit, policy.conversions())
    return None if factor is None else qty * factor


def resolve_po(data: MatchInput) -> PurchaseOrder | None:
    """The one purchase order the invoice names, or None."""
    wanted = norm_id(data.invoice.po_number)
    found = [p for p in data.purchase_orders if norm_id(p.po_number) == wanted]
    return found[0] if wanted is not None and len(found) == 1 else None


def unknown_line_events(data: MatchInput, po: PurchaseOrder) -> list[ReceivingEvent]:
    """Events for this PO that name a line it does not have: attached to no line."""
    lines = {norm_id(ln.line_number) for ln in po.lines}
    return [
        ev
        for ev in data.receiving_events
        if norm_id(ev.po_number) == norm_id(po.po_number)
        and norm_id(ev.po_line_number) not in lines
    ]


def positions(data: MatchInput, policy: ReconciliationPolicy) -> list[LinePosition]:
    """The position of every line of the purchase order the invoice names."""
    po = resolve_po(data)
    if po is None:
        return []
    out = []
    for ln in po.lines:
        unit = canonical_unit(ln.unit, policy.unit_aliases)
        if unit is not None:
            out.append(position(data, po, ln, unit, policy))
    return out


def position(
    data: MatchInput,
    po: PurchaseOrder,
    po_line: POLine,
    po_unit: str,
    policy: ReconciliationPolicy,
) -> LinePosition:
    line = norm_id(po_line.line_number)
    po_key = norm_id(po.po_number)
    problems: list[EventProblem] = []
    receipts = [gr for gr in data.receipts if norm_id(gr.po_number) == po_key]
    by_number: dict[str | None, GoodsReceipt] = {norm_id(gr.receipt_number): gr for gr in receipts}

    delivered = ZERO
    unstated = False
    returned_after = ZERO
    accepted_by_receipt: dict[str | None, Decimal] = defaultdict(lambda: ZERO)
    rejected_by_receipt: dict[str | None, Decimal] = defaultdict(lambda: ZERO)
    rejections: list[Rejection] = []
    accepted_evidence: list[SourceRef] = []
    n_lines = 0
    for gr in receipts:
        key = norm_id(gr.receipt_number)
        for rl in gr.lines:
            if norm_id(rl.po_line_number) != line:
                continue
            n_lines += 1
            if rl.status == "reversed":
                continue
            qty = _convert(rl.quantity, rl.unit, po_unit, policy)
            refs = tuple(
                rl.refs[k]
                for k in ("receipt_line.quantity", "receipt_line.status", "receipt_line.reason")
                if k in rl.refs
            )
            if qty is None:
                problems.append(
                    EventProblem(
                        "needs_review",
                        "unit_unknown",
                        f"receipt {gr.receipt_number} line for PO line {po_line.line_number} is "
                        f"in unit '{rl.unit}', which policy {policy.ref} cannot convert to "
                        f"{po_unit}",
                        refs + tuple(r for k, r in rl.refs.items() if k == "receipt_line.unit"),
                    )
                )
                continue
            if rl.kind == "return":
                if rl.status == "accepted":
                    returned_after += qty
                continue
            delivered += qty
            if rl.status == "accepted":
                accepted_by_receipt[key] += qty
                accepted_evidence += [r for r in refs if r.field == "receipt_line.quantity"]
            elif rl.status == "rejected":
                rejected_by_receipt[key] += qty
                rejections.append(
                    Rejection(
                        gr.receipt_number,
                        gr.receipt_number,
                        qty,
                        rl.reason,
                        gr.received_date.isoformat(),
                        False,
                        refs,
                    )
                )
            else:  # unstated: delivered, acceptance unknown
                unstated = True

    events = [
        ev
        for ev in data.receiving_events
        if norm_id(ev.po_number) == po_key and norm_id(ev.po_line_number) == line
    ]
    all_ids = {
        norm_id(ev.event_id): ev for ev in data.receiving_events if norm_id(ev.po_number) == po_key
    }
    qty_of: dict[str | None, Decimal] = {}
    usable: list[ReceivingEvent] = []
    for ev in events:
        refs = event_refs(ev)
        if ev.quantity is None:
            if ev.event_type in _QUANTIFIED:
                problems.append(
                    EventProblem(
                        "needs_review",
                        "event_quantity_missing",
                        f"{ev.event_type.replace('_', ' ')} {ev.event_id} states no quantity",
                        refs,
                    )
                )
                continue
        else:
            qty = _convert(ev.quantity, ev.unit, po_unit, policy)
            if qty is None:
                problems.append(
                    EventProblem(
                        "needs_review",
                        "unit_unknown",
                        f"{ev.event_type.replace('_', ' ')} {ev.event_id} is in unit "
                        f"'{ev.unit}', which policy {policy.ref} cannot convert to {po_unit}",
                        refs,
                    )
                )
                continue
            qty_of[norm_id(ev.event_id)] = qty
        problem = _reference_problem(ev, by_number, all_ids, line, rejected_by_receipt)
        if problem is not None:
            problems.append(problem)
            continue
        usable.append(ev)

    # Late rejections: units move from accepted (on the receipt named) to rejected.
    late_by_receipt: dict[str | None, Decimal] = defaultdict(lambda: ZERO)
    applied: list[ReceivingEvent] = []
    for ev in usable:
        if ev.event_type != "rejection":
            continue
        key = norm_id(ev.receipt_number)
        qty = qty_of[norm_id(ev.event_id)]
        if late_by_receipt[key] + qty > accepted_by_receipt[key]:
            room = accepted_by_receipt[key] - late_by_receipt[key]
            problems.append(
                EventProblem(
                    "needs_review",
                    "event_quantity_exceeds_reference",
                    f"rejection {ev.event_id} is for {decimal_str(qty)} {po_unit}, more than the "
                    f"{decimal_str(room)} {po_unit} receipt {ev.receipt_number} accepted on PO "
                    f"line {po_line.line_number}",
                    event_refs(ev),
                    {"event_quantity": decimal_str(qty), "received_accepted": decimal_str(room)},
                )
            )
            continue
        late_by_receipt[key] += qty
        rejections.append(
            Rejection(
                ev.event_id, ev.receipt_number, qty, ev.reason, ev.occurred, True, event_refs(ev)
            )
        )
        applied.append(ev)
    rejected = sum((r.quantity for r in rejections), ZERO)

    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    children: dict[tuple[str | None, str], Decimal] = defaultdict(lambda: ZERO)
    credits_elsewhere: list[str] = []
    remedy_evidence: list[SourceRef] = []
    unquantified = {"credit_promised": False, "replacement_promised": False}
    invoice_key = norm_id(data.invoice.invoice_number)
    for ev in usable:
        if ev.event_type == "rejection":
            continue
        qty = qty_of.get(norm_id(ev.event_id))
        if qty is None:  # a promise that states no quantity
            unquantified[ev.event_type] = True
            applied.append(ev)
            remedy_evidence += event_refs(ev)
            continue
        if ev.event_type == "credit_posted" and norm_id(ev.invoice_number) != invoice_key:
            credits_elsewhere.append(ev.invoice_number or "no invoice named")
            applied.append(ev)
            remedy_evidence += event_refs(ev)
            continue
        if ev.follows is not None:
            parent = all_ids[norm_id(ev.follows)]
            group = ev.event_type  # a promise and its posting are stages, not two remedies
            parent_qty = qty_of.get(norm_id(parent.event_id))
            if (
                parent_qty is not None
                and children[(norm_id(parent.event_id), group)] + qty > parent_qty
            ):
                problems.append(_exceeds(ev, qty, parent.event_id, parent_qty, po_unit))
                continue
            children[(norm_id(parent.event_id), group)] += qty
        totals[ev.event_type] += qty
        applied.append(ev)
        remedy_evidence += event_refs(ev)

    # Line totals no remedy may exceed: a unit is returned, replaced or credited at most once.
    caps = (
        ("return", totals["return"], rejected, "rejected"),
        ("replacement_delivery", totals["replacement_delivery"], rejected, "rejected"),
        (
            "replacement_acceptance",
            totals["replacement_acceptance"],
            totals["replacement_delivery"],
            "delivered as replacements",
        ),
        ("credit_posted", totals["credit_posted"], rejected, "rejected"),
    )
    for kind, total, room, what in caps:
        if total > room:
            problems.append(
                EventProblem(
                    "needs_review",
                    "event_quantity_exceeds_reference",
                    f"{kind.replace('_', ' ')} events on PO line {po_line.line_number} total "
                    f"{decimal_str(total)} {po_unit}, more than the {decimal_str(room)} {po_unit} "
                    f"{what}",
                    tuple(r for ev in applied if ev.event_type == kind for r in event_refs(ev)),
                    {"event_quantity": decimal_str(total), "reference_quantity": decimal_str(room)},
                )
            )

    accepted_total = sum(accepted_by_receipt.values(), ZERO) - sum(late_by_receipt.values(), ZERO)
    return LinePosition(
        po_number=po.po_number,
        po_line_number=po_line.line_number,
        unit=po_unit,
        ordered=po_line.quantity,
        delivered=delivered if n_lines else None,
        accepted=None if (unstated or not n_lines) else accepted_total,
        rejected=rejected,
        returned_after_acceptance=returned_after,
        returned=totals["return"],
        replacement_promised=totals["replacement_promised"],
        replacement_delivered=totals["replacement_delivery"],
        replacement_accepted=totals["replacement_acceptance"],
        credit_promised=totals["credit_promised"],
        credit_posted=totals["credit_posted"],
        credits_elsewhere=tuple(credits_elsewhere),
        rejections=tuple(rejections),
        applied=tuple(applied),
        problems=tuple(problems),
        receipt_lines=n_lines,
        accepted_evidence=tuple(accepted_evidence),
        remedy_evidence=tuple(dict.fromkeys(remedy_evidence)),
        promised_credit_unquantified=unquantified["credit_promised"],
        promised_replacement_unquantified=unquantified["replacement_promised"],
    )


def _exceeds(
    ev: ReceivingEvent, qty: Decimal, parent: str, room: Decimal, unit: str
) -> EventProblem:
    return EventProblem(
        "needs_review",
        "event_quantity_exceeds_reference",
        f"{ev.event_type.replace('_', ' ')} {ev.event_id} is for {decimal_str(qty)} {unit}, more "
        f"than {parent} can account for ({decimal_str(room)} {unit})",
        event_refs(ev),
        {"event_quantity": decimal_str(qty), "reference_quantity": decimal_str(room)},
    )


def _reference_problem(
    ev: ReceivingEvent,
    receipts: dict[str | None, GoodsReceipt],
    events: dict[str | None, ReceivingEvent],
    line: str | None,
    rejected_on_receipt: dict[str | None, Decimal],
) -> EventProblem | None:
    """None when what `ev` refers to is in the input, on its PO line, and of the right kind."""
    refs = event_refs(ev)
    name = f"{ev.event_type.replace('_', ' ')} {ev.event_id}"
    if ev.event_type == "rejection":
        if ev.receipt_number is None:
            return EventProblem(
                "needs_review",
                "event_reference_missing",
                f"{name} does not name the receipt whose units it rejects",
                refs,
            )
        if norm_id(ev.receipt_number) not in receipts:
            return _unresolved(name, f"receiving record {ev.receipt_number}", refs)
        return None
    if ev.follows is not None:
        parent = events.get(norm_id(ev.follows))
        if parent is None:
            return _unresolved(name, f"event {ev.follows}", refs)
        if (
            parent.event_type not in _PARENTS[ev.event_type]
            or norm_id(parent.po_line_number) != line
        ):
            return EventProblem(
                "needs_review",
                "event_reference_mismatch",
                f"{name} follows {parent.event_type.replace('_', ' ')} {parent.event_id} on PO "
                f"line {parent.po_line_number}; a {ev.event_type.replace('_', ' ')} can follow "
                + " or ".join(p.replace("_", " ") for p in _PARENTS[ev.event_type])
                + " on the same line",
                refs,
            )
        return None
    if ev.receipt_number is not None and ev.event_type != "replacement_acceptance":
        key = norm_id(ev.receipt_number)
        if key not in receipts:
            return _unresolved(name, f"receiving record {ev.receipt_number}", refs)
        late = any(
            e.event_type == "rejection"
            and norm_id(e.receipt_number) == key
            and norm_id(e.po_line_number) == line
            for e in events.values()
        )
        if not rejected_on_receipt.get(key) and not late:
            return EventProblem(
                "needs_review",
                "event_reference_mismatch",
                f"{name} refers to receipt {ev.receipt_number}, which records no rejected units "
                "on this PO line",
                refs,
            )
        return None
    return EventProblem(
        "needs_review",
        "event_reference_missing",
        f"{name} names neither the event it follows nor the receipt it concerns",
        refs,
    )


def _unresolved(name: str, what: str, refs: tuple[SourceRef, ...]) -> EventProblem:
    return EventProblem(
        "needs_information",
        "event_reference_unresolved",
        f"{name} refers to {what}, which is not in the authorized sources; it was not applied",
        refs,
    )
