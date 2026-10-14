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
from decimal import Decimal
from typing import Literal

from .money import decimal_str, quantize
from .policy import ReconciliationPolicy
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

RULES_VERSION = "opsbridge-rules/1.0.0"

FindingType = Literal[
    "missing_receipt",
    "quantity_mismatch",
    "price_mismatch",
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


def _norm_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip().upper()
    return v or None


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


def _outcome(findings: list[Finding]) -> Outcome:
    statuses = {f.status for f in findings}
    if "needs_review" in statuses:
        return "manual_review"
    if "needs_information" in statuses:
        return "needs_information"
    if "discrepancy" in statuses:
        return "discrepancies_found"
    return "matched"


def reconcile(data: MatchInput, policy: ReconciliationPolicy) -> MatchResult:
    """Evaluate an invoice against its PO, receipts and prior allocations."""
    findings: list[Finding] = []
    invoice = data.invoice

    def done(po: str | None = None, supplier: str | None = None) -> MatchResult:
        return MatchResult(
            outcome=_outcome(findings),
            findings=tuple(findings),
            policy_ref=policy.ref,
            rules_version=RULES_VERSION,
            resolved_po_number=po,
            resolved_supplier_id=supplier,
        )

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

    # 4. Resolve invoice lines to PO lines by exact identifiers.
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
                values={"charges_total": decimal_str(total)},
                evidence=tuple(r for c in charges for r in c.refs.values()),
            )
        )

    received_by_line: dict[str, Decimal] = {}
    for po_line_number, inv_lines in by_po_line.items():
        po_line = po_lines[_norm_id(po_line_number)]
        received = _check_line(data, policy, invoice, po, po_line, inv_lines, inv_ccy, findings)
        if received is not None:
            received_by_line[_norm_id(po_line.line_number) or ""] = received

    # 5. Supplier statements that conflict with receiving records.
    _check_assertions(data, policy, po, received_by_line, findings)

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


def _check_line(
    data: MatchInput,
    policy: ReconciliationPolicy,
    invoice: Invoice,
    po: PurchaseOrder,
    po_line: POLine,
    inv_lines: list[InvoiceLine],
    currency: str,
    findings: list[Finding],
) -> Decimal | None:
    """Quantity and price checks for one PO line. Returns net received qty if known."""
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
    receipt_refs: list[SourceRef] = []
    for _gr, rl in receipt_lines:
        if rl.status != "accepted":
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
            detail = (
                f"{decimal_str(prior_allocated)} of the {decimal_str(net_received)} received were "
                "already matched to earlier invoices"
                if prior_allocated > 0
                else f"only {decimal_str(net_received)} were received"
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
                    evidence=base_refs + tuple(receipt_refs) + prior_refs,
                )
            )

    if prior_allocated + invoiced > po_line.quantity:
        findings.append(
            Finding(
                "quantity_mismatch",
                "discrepancy",
                "invoiced_exceeds_ordered",
                f"Cumulative invoiced quantity {decimal_str(prior_allocated + invoiced)} {po_unit} "
                f"exceeds the ordered {decimal_str(po_line.quantity)} {po_unit} on PO line {line_no}.",
                po_line_number=line_no,
                invoice_line_numbers=inv_line_nos,
                values=dict(values),
                evidence=base_refs + prior_refs,
            )
        )

    for il, factor in price_inputs:
        _check_price(policy, po, po_line, il, factor, currency, findings)

    return net_received if receipt_lines else None


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
    received_by_line: dict[str, Decimal],
    findings: list[Finding],
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
        received = received_by_line[_norm_id(line_no) or ""]
        if assertion.quantity == received:
            continue
        values["net_received"] = decimal_str(received)
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
