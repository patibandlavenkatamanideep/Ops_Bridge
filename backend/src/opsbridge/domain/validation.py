"""What a canonical record must be before any rule reads it.

One set of checks, used at both ends: the pack mapping does not build a record that fails
them (the document is reported as needing review), and `reconcile` refuses to conclude from
input that fails them however it was built. A rule that met a record with no lines found
nothing wrong with it, so "nothing assessed" read as "matched".

Checked: identifiers present and within their stored length; statuses and kinds from the
supported set; quantities, prices and discounts finite Decimals within the input contract;
discounts neither negative nor above the line; at least one line; no two lines (or, across
the input, two receipts) whose identities normalize to the same key. Negative quantities and
prices are left to the matcher's own `negative_value` rule (credit notes are not modelled).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any, get_args, get_type_hints

from .money import MAX_FRACTION_DIGITS, MAX_INTEGER_DIGITS
from .records import (
    GoodsReceipt,
    Invoice,
    InvoiceAllocation,
    InvoiceLine,
    MatchInput,
    POLine,
    PurchaseOrder,
    ReceiptLine,
    SourceRef,
)

# Stored column lengths (persistence/models.py): a longer value cannot be kept, so it is
# refused here as a problem instead of failing the database write.
LINE_ID_MAX = 32
DOCUMENT_ID_MAX = 100
UNIT_MAX = 32


@dataclass(frozen=True)
class Problem:
    """One reason a record cannot be used, with the evidence it was read from."""

    code: str  # identifier_invalid, status_unsupported, value_unsupported, ...
    detail: str
    evidence: tuple[SourceRef, ...] = ()


def norm_id(value: str | None) -> str | None:
    """The matcher's identity key: trimmed and upper-cased."""
    if value is None:
        return None
    v = value.strip().upper()
    return v or None


@cache
def _allowed(cls: type, name: str) -> frozenset[str]:
    """The values a record's Literal field admits (records.py is the one definition)."""
    return frozenset(get_args(get_type_hints(cls)[name]))


def _ref(refs: dict[str, SourceRef], key: str) -> tuple[SourceRef, ...]:
    ref = refs.get(key)
    return (ref,) if ref is not None else ()


class _Checks:
    def __init__(self) -> None:
        self.found: list[Problem] = []

    def add(self, code: str, detail: str, evidence: tuple[SourceRef, ...] = ()) -> None:
        self.found.append(Problem(code, detail, evidence))

    def identifier(
        self, what: str, value: Any, limit: int, evidence: tuple[SourceRef, ...] = ()
    ) -> None:
        if not isinstance(value, str) or not value.strip():
            self.add("identifier_invalid", f"{what} is missing", evidence)
        elif len(value) > limit or any(ord(c) < 32 for c in value):
            self.add(
                "identifier_invalid",
                f"{what} {value[:40]!r} is longer than {limit} characters or has control "
                "characters",
                evidence,
            )

    def optional_identifier(
        self, what: str, value: Any, limit: int, evidence: tuple[SourceRef, ...] = ()
    ) -> None:
        if value is not None:
            self.identifier(what, value, limit, evidence)

    def member(
        self, what: str, value: Any, cls: type, name: str, evidence: tuple[SourceRef, ...] = ()
    ) -> None:
        allowed = _allowed(cls, name)
        if value not in allowed:
            self.add(
                "status_unsupported",
                f"{what} {value!r} is not one of {', '.join(sorted(allowed))}",
                evidence,
            )

    def amount(self, what: str, value: Any, evidence: tuple[SourceRef, ...] = ()) -> bool:
        """True when `value` is a Decimal the matcher can compute with exactly."""
        if isinstance(value, bool) or not isinstance(value, Decimal):
            self.add("value_unsupported", f"{what} is not an exact decimal ({value!r})", evidence)
            return False
        if not value.is_finite():
            self.add("value_unsupported", f"{what} is not a finite number ({value})", evidence)
            return False
        _sign, digits, exponent = value.as_tuple()
        assert isinstance(exponent, int)  # finite
        fraction = max(0, -exponent)
        integer = max(0, len(digits) + exponent)
        if integer > MAX_INTEGER_DIGITS or fraction > MAX_FRACTION_DIGITS:
            self.add(
                "value_unsupported",
                f"{what} {value} exceeds {MAX_INTEGER_DIGITS} integer or "
                f"{MAX_FRACTION_DIGITS} decimal digits",
                evidence,
            )
            return False
        return True

    def discount(
        self,
        what: str,
        quantity: Any,
        price: Any,
        discount: Any,
        evidence: tuple[SourceRef, ...],
    ) -> None:
        ok = all(
            [
                self.amount(f"{what} quantity", quantity, evidence),
                self.amount(f"{what} unit price", price, evidence),
                self.amount(f"{what} discount", discount, evidence),
            ]
        )
        if not ok:
            return
        if discount < 0:
            self.add("discount_unsupported", f"{what} discount {discount} is negative", evidence)
        elif quantity >= 0 and price >= 0 and discount > quantity * price:
            self.add(
                "discount_unsupported",
                f"{what} discount {discount} exceeds the line amount {quantity * price}",
                evidence,
            )

    def unique_lines(self, what: str, ids: Iterable[tuple[str, tuple[SourceRef, ...]]]) -> None:
        groups: dict[str, list[tuple[str, tuple[SourceRef, ...]]]] = defaultdict(list)
        for raw, evidence in ids:
            key = norm_id(raw) if isinstance(raw, str) else None
            if key is not None:
                groups[key].append((raw, evidence))
        for key, members in groups.items():
            if len(members) > 1:
                self.add(
                    "line_identity_duplicate",
                    f"{what} has {len(members)} lines identified as {key} ("
                    + ", ".join(repr(raw) for raw, _ in members)
                    + ")",
                    tuple(r for _, ev in members for r in ev),
                )


def _po_line(c: _Checks, po: PurchaseOrder, line: POLine) -> None:
    what = f"PO {po.po_number} line {line.line_number}"
    c.identifier(f"{po.po_number} line number", line.line_number, LINE_ID_MAX)
    c.optional_identifier(f"{what} item code", line.item_code, DOCUMENT_ID_MAX)
    c.identifier(f"{what} unit", line.unit, UNIT_MAX, _ref(line.refs, "po_line.unit"))
    evidence = _ref(line.refs, "po_line.quantity") + _ref(line.refs, "po_line.unit_price")
    c.discount(what, line.quantity, line.unit_price, line.discount_amount, evidence)


def purchase_order_problems(po: PurchaseOrder) -> list[Problem]:
    c = _Checks()
    c.identifier("PO number", po.po_number, DOCUMENT_ID_MAX)
    c.identifier(f"PO {po.po_number} supplier id", po.supplier_id, DOCUMENT_ID_MAX)
    c.member(f"PO {po.po_number} status", po.status, PurchaseOrder, "status")
    if not po.lines:
        c.add("record_lines_missing", f"PO {po.po_number} has no lines")
    for line in po.lines:
        _po_line(c, po, line)
    c.unique_lines(
        f"PO {po.po_number}",
        ((ln.line_number, _ref(ln.refs, "po_line.quantity")) for ln in po.lines),
    )
    return c.found


def _invoice_line(c: _Checks, inv: Invoice, line: InvoiceLine) -> None:
    what = f"invoice {inv.invoice_number} line {line.line_number}"
    c.identifier(f"invoice {inv.invoice_number} line number", line.line_number, LINE_ID_MAX)
    c.optional_identifier(
        f"{what} PO line reference",
        line.po_line_number,
        LINE_ID_MAX,
        _ref(line.refs, "invoice_line.po_line_number"),
    )
    c.optional_identifier(f"{what} item code", line.item_code, DOCUMENT_ID_MAX)
    c.identifier(f"{what} unit", line.unit, UNIT_MAX, _ref(line.refs, "invoice_line.unit"))
    c.member(f"{what} kind", line.kind, InvoiceLine, "kind")
    evidence = _ref(line.refs, "invoice_line.quantity") + _ref(line.refs, "invoice_line.unit_price")
    c.discount(what, line.quantity, line.unit_price, line.discount_amount, evidence)


def invoice_problems(inv: Invoice) -> list[Problem]:
    c = _Checks()
    c.identifier("invoice number", inv.invoice_number, DOCUMENT_ID_MAX)
    c.optional_identifier(f"invoice {inv.invoice_number} supplier id", inv.supplier_id, 100)
    c.optional_identifier(f"invoice {inv.invoice_number} PO reference", inv.po_number, 100)
    c.member(
        f"invoice {inv.invoice_number} status",
        inv.status,
        Invoice,
        "status",
        _ref(inv.refs, "invoice.status"),
    )
    if not inv.lines:
        c.add("record_lines_missing", f"invoice {inv.invoice_number} has no lines")
    for line in inv.lines:
        _invoice_line(c, inv, line)
    c.unique_lines(
        f"invoice {inv.invoice_number}",
        ((ln.line_number, _ref(ln.refs, "invoice_line.quantity")) for ln in inv.lines),
    )
    return c.found


def _receipt_line(c: _Checks, gr: GoodsReceipt, line: ReceiptLine) -> None:
    what = f"receipt {gr.receipt_number} line for PO line {line.po_line_number}"
    evidence = _ref(line.refs, "receipt_line.quantity")
    c.identifier(f"receipt {gr.receipt_number} PO line", line.po_line_number, LINE_ID_MAX)
    c.identifier(f"{what} unit", line.unit, UNIT_MAX, _ref(line.refs, "receipt_line.unit"))
    c.member(f"{what} kind", line.kind, ReceiptLine, "kind", evidence)
    c.member(f"{what} status", line.status, ReceiptLine, "status", evidence)
    c.amount(f"{what} quantity", line.quantity, evidence)


def receipt_problems(gr: GoodsReceipt) -> list[Problem]:
    c = _Checks()
    c.identifier("receipt number", gr.receipt_number, DOCUMENT_ID_MAX)
    c.identifier(f"receipt {gr.receipt_number} PO number", gr.po_number, DOCUMENT_ID_MAX)
    if not gr.lines:
        c.add("record_lines_missing", f"receipt {gr.receipt_number} has no lines")
    for line in gr.lines:
        _receipt_line(c, gr, line)
    return c.found


def allocation_problems(a: InvoiceAllocation) -> list[Problem]:
    c = _Checks()
    what = f"allocation of {a.invoice_number} to PO line {a.po_line_number}"
    evidence = _ref(a.refs, "allocation.quantity")
    c.identifier("allocation invoice number", a.invoice_number, DOCUMENT_ID_MAX)
    c.identifier(f"{what} PO number", a.po_number, DOCUMENT_ID_MAX)
    c.identifier(f"{what} PO line", a.po_line_number, LINE_ID_MAX)
    c.member(f"{what} status", a.status, InvoiceAllocation, "status", evidence)
    c.amount(f"{what} quantity", a.quantity, evidence)
    return c.found


def match_input_problems(data: MatchInput) -> list[Problem]:
    """Every reason the matcher cannot conclude from `data`; empty when it can."""
    found = invoice_problems(data.invoice)
    for po in data.purchase_orders:
        found += purchase_order_problems(po)
    for gr in data.receipts:
        found += receipt_problems(gr)
    for a in data.prior_allocations:
        found += allocation_problems(a)
    c = _Checks()
    for s in data.supplier_assertions:
        c.amount(f"supplier claim for {s.po_number}", s.quantity, tuple(s.refs.values()))
    # The same receipt twice would count its quantity twice; several lines for one PO line
    # within a receipt are ordinary (split deliveries) and stay allowed.
    by_number: dict[str, list[GoodsReceipt]] = defaultdict(list)
    for gr in data.receipts:
        key = norm_id(gr.receipt_number) if isinstance(gr.receipt_number, str) else None
        if key is not None:
            by_number[key].append(gr)
    for key, receipts in by_number.items():
        if len(receipts) > 1:
            c.add(
                "record_identity_duplicate",
                f"receipt {key} appears {len(receipts)} times in the input",
                tuple(r for gr in receipts for r in gr.refs.values()),
            )
    return found + c.found
