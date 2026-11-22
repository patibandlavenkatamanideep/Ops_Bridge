"""Deterministic three-way matching: purchase order, goods receipt, invoice.

Pure functions only. The matcher never guesses: anything it cannot evaluate exactly
(unknown unit, unsupported currency, uncertain supplier or line identity, undefined
source precedence, zero-price percentage tolerance) becomes an explicit
`needs_review` finding that names the condition. Missing records become
`needs_information`, never evidence that goods were not delivered.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Literal

from .money import MATCH_CONTEXT, decimal_str, quantize
from .policy import ReconciliationPolicy
from .receiving import LinePosition, event_refs, position, unknown_line_events
from .records import (
    Invoice,
    InvoiceLine,
    MatchInput,
    POLine,
    PurchaseOrder,
    SourceRef,
    Supplier,
)
from .units import canonical_unit, conversion_factor
from .validation import Problem, match_input_problems, receiving_events_problems
from .validation import norm_id as _norm_id

# 1.4.0 reads the receiving position (receiving.py, docs/receiving-disposition.md): delivered,
# accepted and rejected are kept apart, receiving events (late rejections, returns,
# replacements, credits) are applied when what they refer to is present, and rejected units a
# shortfall is made of become a `disposition` finding for the policy's disposition owner, not a
# delivery question for receiving. 1.3.0 counts receipt lines rejected at receiving: a
# shortfall says how many were delivered and rejected and cites them, and a supplier's delivery
# claim equal to the accepted plus the rejected units agrees with receiving instead of
# conflicting with it. 1.2.0 skipped rejected lines without a word. Runs recorded under an
# earlier version replay with it.
RULES_VERSION = "opsbridge-rules/1.4.0"
REJECTIONS_RULES_VERSION = "opsbridge-rules/1.3.0"
SILENT_REJECTIONS_RULES_VERSION = "opsbridge-rules/1.2.0"
RULES_VERSIONS = (SILENT_REJECTIONS_RULES_VERSION, REJECTIONS_RULES_VERSION, RULES_VERSION)

FindingType = Literal[
    "missing_receipt",
    "quantity_mismatch",
    "price_mismatch",
    "disposition",
    "source_conflict",
    "manual_review",
    "note",
]
FindingStatus = Literal["discrepancy", "needs_information", "needs_review", "informational"]
Outcome = Literal["matched", "discrepancies_found", "needs_information", "manual_review"]


@dataclass(frozen=True)
class Finding:
    finding_type: FindingType
    status: FindingStatus
    reason_code: str
    summary: str
    po_line_number: str | None = None
    invoice_line_numbers: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)
    evidence: tuple[SourceRef, ...] = ()

    @property
    def finding_id(self) -> str:
        scope = self.po_line_number or "header"
        return f"{self.finding_type}:{scope}:{self.reason_code}"

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "finding_type": self.finding_type,
            "status": self.status,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "po_line_number": self.po_line_number,
            "invoice_line_numbers": list(self.invoice_line_numbers),
            "values": dict(self.values),
            "evidence": [ref.to_dict() for ref in self.evidence],
        }


@dataclass(frozen=True)
class MatchResult:
    outcome: Outcome
    findings: tuple[Finding, ...]
    policy_ref: str
    rules_version: str
    resolved_po_number: str | None
    resolved_supplier_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "policy_ref": self.policy_ref,
            "rules_version": self.rules_version,
            "resolved_po_number": self.resolved_po_number,
            "resolved_supplier_id": self.resolved_supplier_id,
            "findings": [f.to_dict() for f in self.findings],
        }

    def of_type(self, finding_type: FindingType) -> list[Finding]:
        return [f for f in self.findings if f.finding_type == finding_type]


_NAME_NOISE = re.compile(r"[^a-z0-9 ]+")


def _norm_name(name: str) -> str:
    return " ".join(_NAME_NOISE.sub(" ", name.lower()).split())


def _refs(*items: tuple[dict[str, SourceRef], str]) -> tuple[SourceRef, ...]:
    out: list[SourceRef] = []
    for refs, key in items:
        ref = refs.get(key)
        if ref is not None and ref not in out:
            out.append(ref)
    return tuple(out)


def _negative_values(
    data: MatchInput, events: bool = False
) -> list[tuple[str, tuple[SourceRef, ...]]]:
    """Each negative quantity or price in the input, named, with its evidence."""
    out = []
    for ev in data.receiving_events if events else ():
        if ev.quantity is not None and ev.quantity < 0:
            out.append(
                (f"receiving event {ev.event_id} quantity {ev.quantity}",
                 tuple(r for k, r in ev.refs.items() if k == "receiving_event.quantity"))
            )  # fmt: skip
    for il in data.invoice.lines:
        if il.quantity < 0:
            out.append(
                (f"invoice line {il.line_number} quantity {il.quantity}",
                 _refs((il.refs, "invoice_line.quantity")))
            )  # fmt: skip
        if il.unit_price < 0:
            out.append(
                (f"invoice line {il.line_number} unit price {il.unit_price}",
                 _refs((il.refs, "invoice_line.unit_price")))
            )  # fmt: skip
    for po in data.purchase_orders:
        for pl in po.lines:
            if pl.quantity < 0 or pl.unit_price < 0:
                what = "quantity" if pl.quantity < 0 else "unit price"
                value = pl.quantity if pl.quantity < 0 else pl.unit_price
                out.append(
                    (f"{po.po_number} line {pl.line_number} {what} {value}",
                     _refs((pl.refs, f"po_line.{'quantity' if what == 'quantity' else 'unit_price'}")))
                )  # fmt: skip
    for receipt in data.receipts:
        for rl in receipt.lines:
            if rl.quantity < 0:
                out.append(
                    (f"{receipt.receipt_number} line {rl.po_line_number} quantity {rl.quantity}",
                     _refs((rl.refs, "receipt_line.quantity")))
                )  # fmt: skip
    for a in data.prior_allocations:
        if a.quantity < 0:
            out.append(
                (f"allocation of {a.invoice_number} to PO line {a.po_line_number} quantity "
                 f"{a.quantity}", _refs((a.refs, "allocation.quantity")))
            )  # fmt: skip
    return out


def _outcome(findings: list[Finding]) -> Outcome:
    statuses = {f.status for f in findings}
    if "needs_review" in statuses:
        return "manual_review"
    if "needs_information" in statuses:
        return "needs_information"
    if "discrepancy" in statuses:
        return "discrepancies_found"
    return "matched"


_INPUT_HEADLINES = {
    "record_lines_missing": "A record has no lines, so nothing on it can be checked",
    "line_identity_duplicate": "Lines share an identity, so which one applies is ambiguous",
    "record_identity_duplicate": "A record appears more than once and would be counted twice",
    "status_unsupported": "A status or kind is outside the supported set",
    "value_unsupported": "A quantity or price is outside the supported input",
    "discount_unsupported": "A discount is negative or exceeds its line",
    "identifier_invalid": "An identifier is missing or cannot be stored",
}


def _input_findings(problems: list[Problem]) -> list[Finding]:
    """One review finding per kind of input problem, naming each occurrence."""
    by_code: dict[str, list[Problem]] = defaultdict(list)
    for p in problems:
        by_code[p.code].append(p)
    out = []
    for code, group in by_code.items():
        details = "; ".join(p.detail for p in group)[:600]
        evidence: list[SourceRef] = []
        for p in group:
            evidence += [r for r in p.evidence if r not in evidence]
        out.append(
            Finding(
                "manual_review",
                "needs_review",
                code,
                f"{_INPUT_HEADLINES[code]}: {details}. Nothing was concluded; a person must "
                "review the source records.",
                values={"problems": details},
                evidence=tuple(evidence),
            )
        )
    return out


def reconcile(
    data: MatchInput, policy: ReconciliationPolicy, rules_version: str = RULES_VERSION
) -> MatchResult:
    """Evaluate an invoice against its PO, receipts and prior allocations, under the rules
    version a run was made with (the current one for a new run)."""
    if rules_version not in RULES_VERSIONS:
        raise ValueError(f"unknown rules version {rules_version!r}")
    with localcontext(MATCH_CONTEXT):
        return _reconcile(data, policy, rules_version)


def _reconcile(data: MatchInput, policy: ReconciliationPolicy, rules_version: str) -> MatchResult:
    findings: list[Finding] = []
    invoice = data.invoice
    rejections = rules_version != SILENT_REJECTIONS_RULES_VERSION
    disposition = rules_version == RULES_VERSION

    def done(po: str | None = None, supplier: str | None = None) -> MatchResult:
        return MatchResult(
            outcome=_outcome(findings),
            findings=tuple(findings),
            policy_ref=policy.ref,
            rules_version=rules_version,
            resolved_po_number=po,
            resolved_supplier_id=supplier,
        )

    # Input the canonical checks refuse (validation.py) is not evaluated at all: a rule that
    # cannot read a record finds nothing wrong with it, and "nothing found" reads as a match.
    problems = match_input_problems(data)
    if disposition:
        problems += receiving_events_problems(data.receiving_events)
    if problems:
        findings.extend(_input_findings(problems))
        return done()

    if invoice.status == "cancelled":
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "invoice_cancelled",
                f"Invoice {invoice.invoice_number} is cancelled in the source system.",
                evidence=_refs((invoice.refs, "invoice.status")),
            )
        )
        return done()

    # A negative quantity or price (a credit note, a reversal posted as a negative line, a
    # mistaken correction) is not modelled: -20 invoiced would count as covered by any
    # receipt and the case would match. Nothing is concluded from such data.
    negative = _negative_values(data, events=disposition)
    if negative:
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "negative_value",
                "Negative quantities and prices are not supported (credit notes are not "
                "modelled): " + "; ".join(text for text, _ in negative) + ". A person must "
                "review this invoice.",
                evidence=tuple(r for _, refs in negative for r in refs),
            )
        )
        return done()

    # 1. Resolve the purchase order by exact identifier.
    po_number = _norm_id(invoice.po_number)
    if po_number is None:
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "po_reference_missing",
                f"Invoice {invoice.invoice_number} does not reference a purchase order.",
                evidence=_refs((invoice.refs, "invoice.po_number")),
            )
        )
        return done()
    matches = [po for po in data.purchase_orders if _norm_id(po.po_number) == po_number]
    if not matches:
        findings.append(
            Finding(
                "manual_review",
                "needs_information",
                "po_record_unavailable",
                f"Purchase order {po_number} referenced by the invoice is not available "
                "in the authorized sources.",
                evidence=_refs((invoice.refs, "invoice.po_number")),
            )
        )
        return done()
    if len(matches) > 1:
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "po_identifier_ambiguous",
                f"{len(matches)} purchase orders share identifier {po_number}.",
                evidence=_refs((invoice.refs, "invoice.po_number")),
            )
        )
        return done()
    po = matches[0]
    if po.status == "cancelled":
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "po_cancelled",
                f"Purchase order {po.po_number} is cancelled.",
                evidence=_refs((po.refs, "purchase_order.status")),
            )
        )
        return done(po.po_number)

    # 2. Resolve the supplier. Identifiers must match exactly; names never join silently.
    supplier_finding = _check_supplier(invoice, po, data.suppliers)
    if supplier_finding is not None:
        findings.append(supplier_finding)
        if supplier_finding.status == "needs_review":
            return done(po.po_number)

    # 3. Currency: no conversion in v1.
    inv_ccy = invoice.currency.strip().upper()
    po_ccy = po.currency.strip().upper()
    if inv_ccy not in policy.supported_currencies or po_ccy not in policy.supported_currencies:
        unsupported = sorted({c for c in (inv_ccy, po_ccy) if c not in policy.supported_currencies})
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "currency_unsupported",
                f"Currency {', '.join(unsupported)} is not supported by policy {policy.ref}.",
                values={"invoice_currency": inv_ccy, "po_currency": po_ccy},
                evidence=_refs(
                    (invoice.refs, "invoice.currency"), (po.refs, "purchase_order.currency")
                ),
            )
        )
        return done(po.po_number, po.supplier_id)
    if inv_ccy != po_ccy:
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "currency_conversion_unsupported",
                f"Invoice currency {inv_ccy} differs from PO currency {po_ccy}; "
                "currency conversion is outside the supported scope.",
                values={"invoice_currency": inv_ccy, "po_currency": po_ccy},
                evidence=_refs(
                    (invoice.refs, "invoice.currency"), (po.refs, "purchase_order.currency")
                ),
            )
        )
        return done(po.po_number, po.supplier_id)

    # 4. Resolve invoice lines to PO lines by exact identifiers (unique: validation.py).
    if all(line.kind == "charge" for line in invoice.lines):
        # Charges are excluded from matching, so nothing on this invoice would be checked.
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "goods_lines_absent",
                f"Invoice {invoice.invoice_number} has only charge lines; charges are not "
                "matched against the purchase order, so a person must review it.",
                invoice_line_numbers=tuple(line.line_number for line in invoice.lines),
                evidence=tuple(r for line in invoice.lines for r in line.refs.values()),
            )
        )
        return done(po.po_number, po.supplier_id)
    po_lines = {_norm_id(line.line_number): line for line in po.lines}
    by_po_line: dict[str, list[InvoiceLine]] = defaultdict(list)
    charges: list[InvoiceLine] = []
    for line in invoice.lines:
        if line.kind == "charge":
            charges.append(line)
            continue
        target = _resolve_po_line(line, po, po_lines, findings)
        if target is not None:
            by_po_line[target.line_number].append(line)

    if charges:
        total = sum((c.quantity * c.unit_price for c in charges), Decimal(0))
        findings.append(
            Finding(
                "note",
                "informational",
                "charges_not_evaluated",
                f"{len(charges)} charge line(s) totalling {decimal_str(total)} {inv_ccy} are "
                "excluded from price and quantity matching.",
                invoice_line_numbers=tuple(c.line_number for c in charges),
                values={"charge_lines": str(len(charges)), "charges_total": decimal_str(total)},
                evidence=tuple(r for c in charges for r in c.refs.values()),
            )
        )

    if disposition:
        for ev in unknown_line_events(data, po):
            findings.append(
                Finding(
                    "manual_review",
                    "needs_review",
                    "event_line_unknown",
                    f"Receiving event {ev.event_id} ({ev.event_type.replace('_', ' ')}) names "
                    f"PO line {ev.po_line_number}, which {po.po_number} does not have; it was "
                    "not attached to any line, and a person must review it.",
                    values={"po_line": ev.po_line_number},
                    evidence=event_refs(ev),
                )
            )

    received_by_line: dict[str, _Received] = {}
    lines: list[_LineResult] = []
    for po_line_number, inv_lines in by_po_line.items():
        po_line = po_lines[_norm_id(po_line_number)]
        received: _Received | None
        if disposition:
            checked = _check_line_position(
                data, policy, invoice, po, po_line, inv_lines, inv_ccy, findings
            )
            if checked is not None:
                lines.append(checked)
            received = checked.received if checked is not None else None
        else:
            received = _check_line(
                data, policy, invoice, po, po_line, inv_lines, inv_ccy, findings, rejections
            )
        if received is not None:
            received_by_line[_norm_id(po_line.line_number) or ""] = received

    # 5. Supplier statements that conflict with receiving records.
    _check_assertions(data, policy, po, received_by_line, findings, rejections)

    if disposition:
        _disposition_owner(policy, findings)
        _hold_note(policy, lines, findings)

    return done(po.po_number, po.supplier_id)


def _check_supplier(
    invoice: Invoice, po: PurchaseOrder, suppliers: tuple[Supplier, ...]
) -> Finding | None:
    inv_sid = _norm_id(invoice.supplier_id)
    po_sid = _norm_id(po.supplier_id)
    if inv_sid is not None:
        if inv_sid == po_sid:
            return None
        return Finding(
            "manual_review",
            "needs_review",
            "supplier_mismatch",
            f"Invoice supplier {invoice.supplier_id} differs from PO supplier {po.supplier_id}.",
            values={"invoice_supplier_id": inv_sid, "po_supplier_id": po_sid or ""},
            evidence=_refs(
                (invoice.refs, "invoice.supplier_id"), (po.refs, "purchase_order.supplier_id")
            ),
        )
    inv_name = _norm_name(invoice.supplier_name)
    if inv_name and inv_name == _norm_name(po.supplier_name):
        return Finding(
            "note",
            "informational",
            "supplier_identified_by_exact_name",
            f"Invoice has no supplier ID; its supplier name exactly matches PO supplier "
            f"{po.supplier_id} after normalization.",
            values={"po_supplier_id": po.supplier_id},
            evidence=_refs(
                (invoice.refs, "invoice.supplier_name"), (po.refs, "purchase_order.supplier_name")
            ),
        )
    known = list(suppliers) or [Supplier(po.supplier_id, po.supplier_name)]
    candidates = sorted(
        ((difflib.SequenceMatcher(None, inv_name, _norm_name(s.name)).ratio(), s) for s in known),
        key=lambda pair: -pair[0],
    )
    similar = [s.supplier_id for score, s in candidates if score >= 0.6]
    return Finding(
        "manual_review",
        "needs_review",
        "supplier_match_uncertain",
        f"Invoice supplier name '{invoice.supplier_name}' has no supplier ID and does not "
        f"exactly match PO supplier '{po.supplier_name}'. Similar suppliers are candidates "
        "only and need confirmation.",
        values={"candidate_supplier_ids": ",".join(similar)},
        evidence=_refs(
            (invoice.refs, "invoice.supplier_name"), (po.refs, "purchase_order.supplier_name")
        ),
    )


def _resolve_po_line(
    line: InvoiceLine,
    po: PurchaseOrder,
    po_lines: dict[str | None, POLine],
    findings: list[Finding],
) -> POLine | None:
    ref_line = _norm_id(line.po_line_number)
    if ref_line is not None:
        target = po_lines.get(ref_line)
        if target is None:
            findings.append(
                Finding(
                    "manual_review",
                    "needs_review",
                    "po_line_not_found",
                    f"Invoice line {line.line_number} references PO line {line.po_line_number}, "
                    f"which does not exist on {po.po_number}.",
                    invoice_line_numbers=(line.line_number,),
                    evidence=_refs((line.refs, "invoice_line.po_line_number")),
                )
            )
        return target
    code = _norm_id(line.item_code)
    candidates = [pl for pl in po.lines if code is not None and _norm_id(pl.item_code) == code]
    if len(candidates) == 1:
        findings.append(
            Finding(
                "note",
                "informational",
                "po_line_matched_by_item_code",
                f"Invoice line {line.line_number} has no PO line number; matched to PO line "
                f"{candidates[0].line_number} by exact item code {line.item_code}.",
                po_line_number=candidates[0].line_number,
                invoice_line_numbers=(line.line_number,),
                evidence=_refs(
                    (line.refs, "invoice_line.item_code"), (candidates[0].refs, "po_line.item_code")
                ),
            )
        )
        return candidates[0]
    findings.append(
        Finding(
            "manual_review",
            "needs_review",
            "po_line_ambiguous" if candidates else "po_line_unresolved",
            f"Invoice line {line.line_number} cannot be tied to exactly one PO line "
            f"({len(candidates)} candidates by item code).",
            invoice_line_numbers=(line.line_number,),
            values={"candidate_po_lines": ",".join(c.line_number for c in candidates)},
            evidence=_refs((line.refs, "invoice_line.item_code")),
        )
    )
    return None


def _to_po_units(
    qty: Decimal, raw_unit: str, po_unit: str, policy: ReconciliationPolicy
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Convert a quantity into the PO line's canonical unit.

    Returns (converted_qty, factor, problem) where problem names the unsupported condition.
    """
    canonical = canonical_unit(raw_unit, policy.unit_aliases)
    if canonical is None:
        return None, None, f"unit_unknown:{raw_unit}"
    factor = conversion_factor(canonical, po_unit, policy.conversions())
    if factor is None:
        return None, None, f"unit_conversion_unsupported:{canonical}->{po_unit}"
    return qty * factor, factor, None


def _net_unit_price(
    unit_price: Decimal, discount_amount: Decimal, quantity: Decimal, policy: ReconciliationPolicy
) -> Decimal | None:
    if quantity == 0:
        return None
    net = unit_price - (discount_amount / quantity)
    return quantize(net, policy.price_rounding.places, policy.price_rounding.mode)


@dataclass(frozen=True)
class _Received:
    """What receiving recorded for one PO line, in PO units."""

    net: Decimal  # accepted receipts minus accepted returns
    rejected: Decimal  # delivered and rejected (counted from rules 1.3.0)
    rejected_refs: tuple[SourceRef, ...]


def _check_line(
    data: MatchInput,
    policy: ReconciliationPolicy,
    invoice: Invoice,
    po: PurchaseOrder,
    po_line: POLine,
    inv_lines: list[InvoiceLine],
    currency: str,
    findings: list[Finding],
    rejections: bool,
) -> _Received | None:
    """Quantity and price checks for one PO line. Returns what was received if known."""
    line_no = po_line.line_number
    inv_line_nos = tuple(il.line_number for il in inv_lines)
    po_unit = canonical_unit(po_line.unit, policy.unit_aliases)
    if po_unit is None:
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "unit_unknown",
                f"PO line {line_no} unit '{po_line.unit}' is not defined in policy {policy.ref}.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values={"unit": po_line.unit},
                evidence=_refs((po_line.refs, "po_line.unit")),
            )
        )
        return None

    # Invoice quantities and prices in PO units.
    invoiced = Decimal(0)
    price_inputs: list[tuple[InvoiceLine, Decimal]] = []
    for il in inv_lines:
        qty, factor, problem = _to_po_units(il.quantity, il.unit, po_unit, policy)
        if problem is not None or qty is None or factor is None:
            findings.append(
                _unit_finding(
                    problem or "unit_unknown",
                    line_no,
                    (il.line_number,),
                    _refs((il.refs, "invoice_line.unit"), (po_line.refs, "po_line.unit")),
                )
            )
            return None
        invoiced += qty
        price_inputs.append((il, factor))

    # Receipts for this PO line: accepted receipts minus accepted returns.
    receipt_lines = [
        (gr, rl)
        for gr in data.receipts
        if _norm_id(gr.po_number) == _norm_id(po.po_number)
        for rl in gr.lines
        if _norm_id(rl.po_line_number) == _norm_id(line_no)
    ]
    received = Decimal(0)
    returned = Decimal(0)
    rejected = Decimal(0)  # delivered, and not counted because receiving rejected it
    receipt_refs: list[SourceRef] = []
    rejected_refs: list[SourceRef] = []
    for _gr, rl in receipt_lines:
        if rl.status != "accepted":
            if rejections and rl.status == "rejected" and rl.kind == "receipt":
                qty, _factor, problem = _to_po_units(rl.quantity, rl.unit, po_unit, policy)
                if problem is None and qty is not None:
                    rejected += qty
                    rejected_refs.extend(
                        _refs((rl.refs, "receipt_line.quantity"), (rl.refs, "receipt_line.status"))
                    )
            continue
        qty, _factor, problem = _to_po_units(rl.quantity, rl.unit, po_unit, policy)
        if problem is not None or qty is None:
            findings.append(
                _unit_finding(
                    problem or "unit_unknown",
                    line_no,
                    inv_line_nos,
                    _refs((rl.refs, "receipt_line.unit"), (po_line.refs, "po_line.unit")),
                )
            )
            return None
        if rl.kind == "return":
            returned += qty
        else:
            received += qty
        receipt_refs.extend(_refs((rl.refs, "receipt_line.quantity")))
    net_received = received - returned

    prior = [
        a
        for a in data.prior_allocations
        if a.status == "active"
        and _norm_id(a.po_number) == _norm_id(po.po_number)
        and _norm_id(a.po_line_number) == _norm_id(line_no)
        and _norm_id(a.invoice_number) != _norm_id(invoice.invoice_number)
    ]
    prior_allocated = sum((a.quantity for a in prior), Decimal(0))
    prior_refs = tuple(r for a in prior for r in _refs((a.refs, "allocation.quantity")))

    base_refs = _refs((po_line.refs, "po_line.quantity")) + tuple(
        r for il in inv_lines for r in _refs((il.refs, "invoice_line.quantity"))
    )
    values = {
        "ordered": decimal_str(po_line.quantity),
        "invoiced": decimal_str(invoiced),
        "received_accepted": decimal_str(received),
        "returned": decimal_str(returned),
        "prior_allocated": decimal_str(prior_allocated),
        "unit": po_unit,
    }
    if rejected:
        values["received_rejected"] = decimal_str(rejected)

    if not receipt_lines:
        findings.append(
            Finding(
                "missing_receipt",
                "needs_information",
                "receiving_record_unavailable",
                f"No receiving record is available for PO {po.po_number} line {line_no}. "
                "This does not establish whether the goods were delivered.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values=values,
                evidence=base_refs,
            )
        )
    else:
        allocatable = net_received - prior_allocated
        tol = policy.quantity_tolerance
        allowed_excess = max(
            tol.absolute, max(allocatable, Decimal(0)) * tol.percent / Decimal(100)
        )
        values.update(
            {
                "net_received": decimal_str(net_received),
                "allocatable": decimal_str(allocatable),
                "allowed_excess": decimal_str(allowed_excess),
            }
        )
        if invoiced > allocatable + allowed_excess:
            excess = invoiced - allocatable
            already_allocated = prior_allocated > 0 and net_received >= invoiced
            reason = (
                "receipt_quantity_already_allocated"
                if already_allocated
                else "invoiced_exceeds_received"
            )
            if prior_allocated > 0:
                detail = (
                    f"{decimal_str(prior_allocated)} of the {decimal_str(net_received)} received "
                    "were already matched to earlier invoices"
                )
            elif rejected and not returned:
                detail = f"only {decimal_str(net_received)} were accepted at receiving"
            else:
                detail = f"only {decimal_str(net_received)} were received"
            if rejected:
                detail += (
                    f" ({decimal_str(rejected)} more were delivered and rejected, and rejected "
                    "units are not counted as received)"
                )
            values["excess"] = decimal_str(excess)
            findings.append(
                Finding(
                    "quantity_mismatch",
                    "discrepancy",
                    reason,
                    f"Invoice bills {decimal_str(invoiced)} {po_unit} on PO line {line_no}, but "
                    f"{detail}; {decimal_str(excess)} {po_unit} exceed the allocatable quantity.",
                    po_line_number=line_no,
                    invoice_line_numbers=inv_line_nos,
                    values=dict(values),
                    evidence=base_refs + tuple(receipt_refs) + tuple(rejected_refs) + prior_refs,
                )
            )

    if prior_allocated + invoiced > po_line.quantity:
        cumulative = prior_allocated + invoiced
        # Own values: every number in the summary is stated here (the receipt shortfall's
        # `excess` belongs to that other finding and is not carried over).
        over = {k: v for k, v in values.items() if k != "excess"}
        over["cumulative_invoiced"] = decimal_str(cumulative)
        over["excess_over_ordered"] = decimal_str(cumulative - po_line.quantity)
        findings.append(
            Finding(
                "quantity_mismatch",
                "discrepancy",
                "invoiced_exceeds_ordered",
                f"Cumulative invoiced quantity {decimal_str(cumulative)} {po_unit} "
                f"exceeds the ordered {decimal_str(po_line.quantity)} {po_unit} on PO line {line_no}.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values=over,
                evidence=base_refs + prior_refs,
            )
        )

    for il, factor in price_inputs:
        _check_price(policy, po, po_line, il, factor, currency, findings)

    if not receipt_lines:
        return None
    return _Received(net_received, rejected, tuple(rejected_refs))


@dataclass(frozen=True)
class _LineResult:
    """One PO line under rules 1.4.0: its billed quantity and how much of it is disputed."""

    line_number: str
    unit: str
    invoiced: Decimal
    disputed: Decimal | None  # None: the line's quantities could not be evaluated
    received: _Received | None


def _check_line_position(
    data: MatchInput,
    policy: ReconciliationPolicy,
    invoice: Invoice,
    po: PurchaseOrder,
    po_line: POLine,
    inv_lines: list[InvoiceLine],
    currency: str,
    findings: list[Finding],
) -> _LineResult | None:
    """Rules 1.4.0 for one PO line: the receiving position (receiving.py) against the invoice."""
    line_no = po_line.line_number
    inv_line_nos = tuple(il.line_number for il in inv_lines)
    po_unit = canonical_unit(po_line.unit, policy.unit_aliases)
    if po_unit is None:
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "unit_unknown",
                f"PO line {line_no} unit '{po_line.unit}' is not defined in policy {policy.ref}.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values={"unit": po_line.unit},
                evidence=_refs((po_line.refs, "po_line.unit")),
            )
        )
        return None
    invoiced = Decimal(0)
    price_inputs: list[tuple[InvoiceLine, Decimal]] = []
    for il in inv_lines:
        qty, factor, problem = _to_po_units(il.quantity, il.unit, po_unit, policy)
        if problem is not None or qty is None or factor is None:
            findings.append(
                _unit_finding(
                    problem or "unit_unknown",
                    line_no,
                    (il.line_number,),
                    _refs((il.refs, "invoice_line.unit"), (po_line.refs, "po_line.unit")),
                )
            )
            return None
        invoiced += qty
        price_inputs.append((il, factor))

    pos = position(data, po, po_line, po_unit, policy)
    prior = [
        a
        for a in data.prior_allocations
        if a.status == "active"
        and _norm_id(a.po_number) == _norm_id(po.po_number)
        and _norm_id(a.po_line_number) == _norm_id(line_no)
        and _norm_id(a.invoice_number) != _norm_id(invoice.invoice_number)
    ]
    prior_allocated = sum((a.quantity for a in prior), Decimal(0))
    prior_refs = tuple(r for a in prior for r in _refs((a.refs, "allocation.quantity")))
    base_refs = _refs((po_line.refs, "po_line.quantity")) + tuple(
        r for il in inv_lines for r in _refs((il.refs, "invoice_line.quantity"))
    )
    rejection_refs = tuple(r for rej in pos.rejections for r in rej.evidence)
    values = {
        **pos.values(),
        "invoiced": decimal_str(invoiced),
        "prior_allocated": decimal_str(prior_allocated),
    }
    if pos.returned_after_acceptance:
        values["returned_after_acceptance"] = decimal_str(pos.returned_after_acceptance)

    disputed: Decimal | None = None
    received: _Received | None = None
    if pos.problems:
        for pr in pos.problems:
            then = (
                " until that record arrives"
                if pr.status == "needs_information"
                else "; a person must review the receiving records"
            )
            findings.append(
                Finding(
                    "missing_receipt" if pr.status == "needs_information" else "manual_review",
                    pr.status,
                    pr.code,
                    f"{pr.detail[0].upper()}{pr.detail[1:]}. Nothing is concluded about the "
                    f"quantities on PO line {line_no}{then}.",
                    po_line_number=line_no,
                    invoice_line_numbers=inv_line_nos,
                    values={**values, **pr.values},
                    evidence=pr.evidence,
                )
            )
    elif pos.receipt_lines == 0:
        findings.append(
            Finding(
                "missing_receipt",
                "needs_information",
                "receiving_record_unavailable",
                f"No receiving record is available for PO {po.po_number} line {line_no}. "
                "This does not establish whether the goods were delivered.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values=values,
                evidence=base_refs,
            )
        )
    elif pos.accepted is None or pos.covered is None:
        findings.append(
            Finding(
                "missing_receipt",
                "needs_information",
                "acceptance_not_stated",
                f"Receiving records {decimal_str(pos.delivered or Decimal(0))} {po_unit} "
                f"delivered on PO line {line_no} but does not say whether they were accepted. "
                "OpsBridge counts units as accepted only when the receiving record says so; ask "
                "receiving for the acceptance.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values=values,
                evidence=base_refs + tuple(r for r in pos.accepted_evidence),
            )
        )
    else:
        covered = pos.covered
        billed = invoiced - pos.credit_posted
        allocatable = covered - prior_allocated
        tol = policy.quantity_tolerance
        allowed_excess = max(
            tol.absolute, max(allocatable, Decimal(0)) * tol.percent / Decimal(100)
        )
        values.update(
            {
                "net_received": decimal_str(covered),
                "allocatable": decimal_str(allocatable),
                "allowed_excess": decimal_str(allowed_excess),
            }
        )
        if pos.credit_posted:
            values["billed_after_credit"] = decimal_str(billed)
        disputed = Decimal(0)
        if billed > allocatable + allowed_excess:
            excess = billed - allocatable
            disputed = excess
            rejected_part = min(excess, pos.open_rejected)
            rest = excess - rejected_part
            if rest > 0:
                findings.append(
                    _shortfall(
                        pos, line_no, inv_line_nos, invoiced, covered, prior_allocated, rest,
                        net_received=covered,
                        values={**values, "excess": decimal_str(rest)},
                        evidence=base_refs + pos.accepted_evidence + rejection_refs + prior_refs,
                    )
                )  # fmt: skip
            if rejected_part > 0:
                findings.append(
                    _disposition_finding(
                        pos,
                        line_no,
                        inv_line_nos,
                        rejected_part,
                        {**values, "disputed": decimal_str(rejected_part)},
                        base_refs + pos.accepted_evidence + rejection_refs + pos.remedy_evidence,
                    )
                )
        elif pos.rejected and pos.open_rejected == 0:
            findings.append(_remedied(pos, line_no, values, rejection_refs + pos.remedy_evidence))
        received = _Received(covered, pos.rejected, rejection_refs)

    if (
        pos.replacement_delivered
        and prior_allocated
        and prior_allocated + invoiced > po_line.quantity
    ):
        ids = ", ".join(
            ev.event_id for ev in pos.applied if ev.event_type == "replacement_delivery"
        )
        earlier = ", ".join(sorted({a.invoice_number for a in prior}))
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "possible_duplicate_replacement_charge",
                f"PO line {line_no} had {decimal_str(pos.replacement_delivered)} {po_unit} "
                f"delivered as replacements for rejected units ({ids}), and "
                f"{decimal_str(prior_allocated)} of the {decimal_str(po_line.quantity)} {po_unit} "
                f"ordered are already allocated to earlier invoices ({earlier}). This invoice "
                f"bills {decimal_str(invoiced)} {po_unit} more on the same line, which may charge "
                "for the replacements; AP must review it before anything is paid.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values={
                    **values,
                    "replacement_events": ids,
                    "earlier_invoices": earlier,
                },
                evidence=tuple(
                    r
                    for ev in pos.applied
                    if ev.event_type == "replacement_delivery"
                    for r in event_refs(ev)
                )
                + prior_refs
                + base_refs,
            )
        )

    if prior_allocated + invoiced > po_line.quantity:
        cumulative = prior_allocated + invoiced
        over = {k: v for k, v in values.items() if k != "excess"}
        over["cumulative_invoiced"] = decimal_str(cumulative)
        over["excess_over_ordered"] = decimal_str(cumulative - po_line.quantity)
        findings.append(
            Finding(
                "quantity_mismatch",
                "discrepancy",
                "invoiced_exceeds_ordered",
                f"Cumulative invoiced quantity {decimal_str(cumulative)} {po_unit} "
                f"exceeds the ordered {decimal_str(po_line.quantity)} {po_unit} on PO line {line_no}.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values=over,
                evidence=base_refs + prior_refs,
            )
        )

    for il, factor in price_inputs:
        _check_price(policy, po, po_line, il, factor, currency, findings)
    return _LineResult(line_no, po_unit, invoiced, disputed, received)


def _shortfall(
    pos: LinePosition,
    line_no: str,
    inv_line_nos: tuple[str, ...],
    invoiced: Decimal,
    covered: Decimal,
    prior_allocated: Decimal,
    rest: Decimal,
    *,
    net_received: Decimal,
    values: dict[str, str],
    evidence: tuple[SourceRef, ...],
) -> Finding:
    """Billed units that neither acceptance nor a rejection accounts for (rules 1.4.0)."""
    unit = pos.unit
    already_allocated = prior_allocated > 0 and net_received >= invoiced
    if prior_allocated > 0:
        detail = (
            f"{decimal_str(prior_allocated)} of the {decimal_str(covered)} accepted were already "
            "matched to earlier invoices"
        )
    elif pos.delivered == covered:
        detail = f"only {decimal_str(covered)} were delivered and accepted"
    else:
        detail = (
            f"{decimal_str(pos.delivered or Decimal(0))} were delivered and only "
            f"{decimal_str(covered)} count as accepted"
        )
    if pos.undelivered:
        detail += f"; {decimal_str(pos.undelivered)} {unit} have not been delivered"
    if pos.rejected:
        detail += (
            f" ({decimal_str(pos.rejected)} more were delivered and rejected; their disposition "
            "is reported on its own)"
        )
    return Finding(
        "quantity_mismatch",
        "discrepancy",
        "receipt_quantity_already_allocated" if already_allocated else "invoiced_exceeds_received",
        f"Invoice bills {decimal_str(invoiced)} {unit} on PO line {line_no}, but {detail}; "
        f"{decimal_str(rest)} {unit} exceed the allocatable quantity.",
        po_line_number=line_no,
        invoice_line_numbers=inv_line_nos,
        values=values,
        evidence=evidence,
    )


def _ids(pos: LinePosition, *types: str) -> str:
    return ", ".join(ev.event_id for ev in pos.applied if ev.event_type in types)


def _disposition_finding(
    pos: LinePosition,
    line_no: str,
    inv_line_nos: tuple[str, ...],
    disputed: Decimal,
    values: dict[str, str],
    evidence: tuple[SourceRef, ...],
) -> Finding:
    """Billed units that were delivered and rejected: what remedy is recorded, and who is
    asked next. A missing fact receiving holds is asked of receiving (needs information); an
    unresolved remedy is a discrepancy for the policy's disposition owner."""
    unit = pos.unit
    sources = []
    for rej in pos.rejections:
        if rej.late:
            sources.append(f"{rej.source}, recorded after receipt {rej.receipt_number}")
        else:
            sources.append(rej.source)
    shown = "; ".join([*pos.reasons, ", ".join(sources)])
    head = (
        f"{decimal_str(disputed)} {unit} delivered on PO line {line_no} were rejected at "
        f"receiving ({shown})"
    )
    elsewhere = (
        f" A posted credit names {', '.join(pos.credits_elsewhere)} instead of this invoice."
        if pos.credits_elsewhere
        else ""
    )
    state = pos.disposition
    if state == "replacement_unaccepted":
        code, status = "replacement_acceptance_unrecorded", "needs_information"
        summary = (
            f"{head}. The supplier delivered {decimal_str(pos.replacement_delivered)} {unit} as "
            f"replacements ({_ids(pos, 'replacement_delivery')}), and the acceptance of "
            f"{decimal_str(pos.replacement_delivered - pos.replacement_accepted)} {unit} of them is "
            "not recorded. Ask receiving whether the replacements were accepted."
        )
    elif any(rej.reason is None for rej in pos.rejections):
        code, status = "rejection_reason_missing", "needs_information"
        summary = (
            f"{head}, and no reason is recorded for the rejection. Ask receiving for the "
            "inspection record before the supplier is asked for a remedy."
        )
    elif state == "credit_pending":
        code, status = "credit_pending", "discrepancy"
        returned = f" They were returned ({_ids(pos, 'return')})." if pos.returned else ""
        summary = (
            f"{head}.{returned} The supplier promised a credit "
            f"({_ids(pos, 'credit_promised')}); no credit note is posted for this invoice, and a "
            f"promise is not a credit.{elsewhere} Ask the supplier for the credit note."
        )
    elif state == "returned":
        code, status = "return_credit_unrecorded", "discrepancy"
        summary = (
            f"{head}. They were returned to the supplier ({_ids(pos, 'return')}); no credit is "
            f"recorded for this invoice.{elsewhere} A return does not show that a credit was "
            "issued; ask the supplier for the credit note."
        )
    elif state == "replacement_pending":
        code, status = "replacement_pending", "discrepancy"
        summary = (
            f"{head}. The supplier promised a replacement ({_ids(pos, 'replacement_promised')}); "
            f"none has arrived.{elsewhere} Ask the supplier when it will be delivered."
        )
    else:
        code, status = "disposition_unrecorded", "discrepancy"
        summary = (
            f"{head}; no return, credit or replacement is recorded.{elsewhere} Ask the supplier "
            "how the rejected units will be remedied."
        )
    return Finding(
        "disposition",
        status,  # type: ignore[arg-type]
        code,
        summary,
        po_line_number=line_no,
        invoice_line_numbers=inv_line_nos,
        values=values,
        evidence=tuple(dict.fromkeys(evidence)),
    )


def _remedied(
    pos: LinePosition, line_no: str, values: dict[str, str], evidence: tuple[SourceRef, ...]
) -> Finding:
    """Rejected units the record shows remedied, for this invoice."""
    unit = pos.unit
    documents = ", ".join(
        ev.document_number or ev.event_id
        for ev in pos.applied
        if ev.event_type == "credit_posted"
        and ev.invoice_number is not None
        and _norm_id(ev.invoice_number) is not None
    )
    parts = []
    if pos.credit_posted:
        parts.append(
            f"{decimal_str(pos.credit_posted)} {unit} by posted credit {documents} for this invoice"
        )
    if pos.replacement_accepted:
        parts.append(
            f"{decimal_str(pos.replacement_accepted)} {unit} by accepted replacements "
            f"({_ids(pos, 'replacement_acceptance')})"
        )
    code = {
        "credited": "rejected_units_credited",
        "replaced": "rejected_units_replaced",
    }.get(pos.disposition, "rejected_units_resolved")
    return Finding(
        "note",
        "informational",
        code,
        f"{decimal_str(pos.rejected)} {unit} rejected on PO line {line_no} are covered: "
        + " and ".join(parts)
        + f". The order stays at {decimal_str(pos.ordered)} {unit}, and the rejection stays in "
        "the record.",
        po_line_number=line_no,
        values=values,
        evidence=tuple(dict.fromkeys(evidence)),
    )


def _disposition_owner(policy: ReconciliationPolicy, findings: list[Finding]) -> None:
    """Without a disposition route, OpsBridge names nobody: a person decides who asks."""
    open_ = [f for f in findings if f.finding_type == "disposition" and f.status == "discrepancy"]
    if not open_ or "disposition" in policy.action_routes:
        return
    findings.append(
        Finding(
            "manual_review",
            "needs_review",
            "disposition_owner_unconfigured",
            f"Policy {policy.ref} names no owner for rejected goods, so OpsBridge does not "
            "choose whom to ask about their disposition. A person must decide who contacts the "
            "supplier.",
            values={"policy": policy.ref},
            evidence=tuple(dict.fromkeys(r for f in open_ for r in f.evidence)),
        )
    )


def _hold_note(
    policy: ReconciliationPolicy, lines: list[_LineResult], findings: list[Finding]
) -> None:
    """The matched and disputed quantities, and what the policy says about holding payment.
    Quantities only: no amount is computed, and nothing is released, short-paid or paid."""
    evaluated = [ln for ln in lines if ln.disputed is not None]
    if not any(ln.disputed for ln in evaluated):
        return
    split = "; ".join(
        f"line {ln.line_number}: {decimal_str(ln.invoiced - (ln.disputed or 0))} {ln.unit} "
        f"match, {decimal_str(ln.disputed or Decimal(0))} {ln.unit} disputed"
        for ln in evaluated
    )
    values = {"split": split, "hold": policy.disputed_quantity_hold or "unconfigured"}
    units = {ln.unit for ln in evaluated}
    if len(units) == 1:
        (unit,) = units
        disputed = sum((ln.disputed or Decimal(0) for ln in evaluated), Decimal(0))
        matched = sum((ln.invoiced for ln in evaluated), Decimal(0)) - disputed
        values.update(
            {
                "matched_quantity": decimal_str(matched),
                "disputed_quantity": decimal_str(disputed),
                "unit": unit,
            }
        )
        shown = (
            f"{decimal_str(matched)} {unit} match and {decimal_str(disputed)} {unit} are disputed"
        )
    else:
        shown = split
    unevaluated = len(lines) - len(evaluated)
    if unevaluated:
        shown += f" ({unevaluated} line(s) could not be evaluated)"
    never = " OpsBridge does not release, short-pay or schedule payment."
    hold = policy.disputed_quantity_hold
    if hold == "whole_invoice":
        code = "hold_whole_invoice"
        summary = (
            f"Policy {policy.ref} holds the whole invoice while any quantity is disputed: {shown}."
        )
    elif hold == "disputed_portion":
        code = "hold_disputed_portion"
        summary = (
            f"Policy {policy.ref} reviews only the disputed portion: {shown}. Amounts are not "
            "computed from quantities."
        )
    else:
        code = "hold_unconfigured"
        summary = (
            f"Policy {policy.ref} does not say whether a disputed quantity holds the whole "
            f"invoice or only the disputed portion: {shown}, and the payment recommendation is "
            "left unresolved."
        )
    findings.append(Finding("note", "informational", code, summary + never, values=values))


def _unit_finding(
    problem: str, line_no: str, inv_line_nos: tuple[str, ...], evidence: tuple[SourceRef, ...]
) -> Finding:
    code, _, detail = problem.partition(":")
    summary = (
        f"Unit '{detail}' is not defined in the policy unit table."
        if code == "unit_unknown"
        else f"No conversion is configured for {detail}; quantities cannot be compared."
    )
    return Finding(
        "manual_review",
        "needs_review",
        code,
        summary,
        po_line_number=line_no,
        invoice_line_numbers=inv_line_nos,
        values={"condition": detail},
        evidence=evidence,
    )


def _check_price(
    policy: ReconciliationPolicy,
    po: PurchaseOrder,
    po_line: POLine,
    il: InvoiceLine,
    factor: Decimal,
    currency: str,
    findings: list[Finding],
) -> None:
    tol = policy.price_tolerance
    po_net = _net_unit_price(po_line.unit_price, po_line.discount_amount, po_line.quantity, policy)
    # Price per invoice unit → price per PO unit: divide by the quantity factor.
    inv_price_po_unit = il.unit_price / factor
    inv_discount = il.discount_amount
    inv_qty_po_units = il.quantity * factor
    inv_net = _net_unit_price(inv_price_po_unit, inv_discount, inv_qty_po_units, policy)
    evidence = _refs(
        (il.refs, "invoice_line.unit_price"),
        (il.refs, "invoice_line.discount_amount"),
        (po_line.refs, "po_line.unit_price"),
        (po_line.refs, "po_line.discount_amount"),
    )
    if po_net is None or inv_net is None:
        findings.append(
            Finding(
                "manual_review",
                "needs_review",
                "price_basis_undefined",
                f"Net unit price cannot be computed for invoice line {il.line_number} "
                "(zero quantity).",
                po_line_number=po_line.line_number,
                invoice_line_numbers=(il.line_number,),
                evidence=evidence,
            )
        )
        return
    diff = inv_net - po_net
    values = {
        "invoice_net_unit_price": decimal_str(inv_net),
        "po_net_unit_price": decimal_str(po_net),
        "difference": decimal_str(diff),
        "currency": currency,
        "tolerance_absolute": decimal_str(tol.absolute) if tol.absolute is not None else "",
        "tolerance_percent": decimal_str(tol.percent) if tol.percent is not None else "",
        "tolerance_mode": tol.mode,
    }
    if diff == 0 or (tol.direction == "over_only" and diff < 0):
        return
    magnitude = abs(diff)
    results: list[bool | None] = []  # None = rule undefined for this input
    if tol.absolute is not None:
        results.append(magnitude <= tol.absolute)
    if tol.percent is not None:
        results.append(None if po_net == 0 else magnitude * 100 <= tol.percent * po_net)
    defined = [r for r in results if r is not None]
    undefined = len(defined) != len(results)
    if tol.mode == "either":
        # Decide on the rules that are defined; review only if none is.
        if any(defined):
            return
        if not defined:
            _zero_price(po_line, il, values, evidence, findings)
            return
    else:  # both: an undefined rule cannot be confirmed to pass
        if undefined:
            _zero_price(po_line, il, values, evidence, findings)
            return
        if all(defined):
            return
    direction = "above" if diff > 0 else "below"
    findings.append(
        Finding(
            "price_mismatch",
            "discrepancy",
            f"price_{direction}_po_beyond_tolerance",
            f"Invoice line {il.line_number} net unit price {decimal_str(inv_net)} {currency} is "
            f"{decimal_str(magnitude)} {direction} the PO price {decimal_str(po_net)} {currency}, "
            f"outside the {policy.ref} tolerance.",
            po_line_number=po_line.line_number,
            invoice_line_numbers=(il.line_number,),
            values=values,
            evidence=evidence,
        )
    )


def _zero_price(
    po_line: POLine,
    il: InvoiceLine,
    values: dict[str, str],
    evidence: tuple[SourceRef, ...],
    findings: list[Finding],
) -> None:
    findings.append(
        Finding(
            "manual_review",
            "needs_review",
            "percent_tolerance_zero_price",
            f"PO line {po_line.line_number} has a zero net unit price, so the percentage "
            "tolerance is undefined.",
            po_line_number=po_line.line_number,
            invoice_line_numbers=(il.line_number,),
            values=values,
            evidence=evidence,
        )
    )


def _check_assertions(
    data: MatchInput,
    policy: ReconciliationPolicy,
    po: PurchaseOrder,
    received_by_line: dict[str, _Received],
    findings: list[Finding],
    rejections: bool,
) -> None:
    precedence = policy.source_precedence
    for assertion in data.supplier_assertions:
        if _norm_id(assertion.po_number) != _norm_id(po.po_number):
            continue
        line_no = assertion.po_line_number
        if line_no is None and len(po.lines) == 1:
            line_no = po.lines[0].line_number
        if line_no is None:
            continue
        evidence = tuple(assertion.refs.values())
        values = {
            "supplier_claimed_delivered": decimal_str(assertion.quantity),
            "extraction_status": assertion.extraction_status,
        }
        if (_norm_id(line_no) or "") not in received_by_line:
            findings.append(
                Finding(
                    "note",
                    "informational",
                    "supplier_claim_unverified",
                    f"Supplier correspondence claims {decimal_str(assertion.quantity)} delivered on "
                    f"PO line {line_no}; no receiving record is available to confirm it.",
                    po_line_number=line_no,
                    values=values,
                    evidence=evidence,
                )
            )
            continue
        line = received_by_line[_norm_id(line_no) or ""]
        received = line.net
        if assertion.quantity == received:
            continue
        values["net_received"] = decimal_str(received)
        if rejections and line.rejected and assertion.quantity == received + line.rejected:
            # Delivered is not accepted: the claim counts the units receiving rejected.
            findings.append(
                Finding(
                    "note",
                    "informational",
                    "supplier_claim_includes_rejected",
                    f"Supplier correspondence claims {decimal_str(assertion.quantity)} delivered "
                    f"on PO line {line_no}, which agrees with receiving: "
                    f"{decimal_str(received)} were accepted and {decimal_str(line.rejected)} "
                    "rejected. Only accepted units are counted as received.",
                    po_line_number=line_no,
                    values={**values, "received_rejected": decimal_str(line.rejected)},
                    evidence=evidence + line.rejected_refs,
                )
            )
            continue
        if "goods_receipt" in precedence and "supplier_message" in precedence:
            if precedence.index("goods_receipt") < precedence.index("supplier_message"):
                findings.append(
                    Finding(
                        "source_conflict",
                        "informational",
                        "conflict_resolved_by_precedence",
                        f"Supplier correspondence claims {decimal_str(assertion.quantity)} delivered "
                        f"but the receiving record shows {decimal_str(received)}. Policy {policy.ref} "
                        "ranks the receiving record above supplier messages, so the receiving record "
                        "controls.",
                        po_line_number=line_no,
                        values={**values, "controlling_source": "goods_receipt"},
                        evidence=evidence,
                    )
                )
                continue
            reason = "supplier_precedence_requires_review"
            summary = (
                "Policy ranks supplier messages above receiving records; OpsBridge does not "
                "accept a supplier claim as receipt evidence, so the conflict needs review."
            )
        else:
            reason = "source_precedence_undefined"
            summary = (
                f"Supplier correspondence claims {decimal_str(assertion.quantity)} delivered but the "
                f"receiving record shows {decimal_str(received)}, and policy {policy.ref} defines no "
                "precedence between these sources."
            )
        findings.append(
            Finding(
                "source_conflict",
                "needs_review",
                reason,
                summary,
                po_line_number=line_no,
                values=values,
                evidence=evidence,
            )
        )
