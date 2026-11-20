"""Canonical procurement records used by the matcher.

Organization packs map their source fields onto these records. Every decisive field
carries a SourceRef so a finding can cite the exact document version and location.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class SourceRef:
    """Pointer to an immutable source document version and a location inside it."""

    document_id: str
    version_id: str
    sha256: str
    locator: str  # e.g. "json:/lines/0/quantity", "csv:row=3;col=qty", "pdf:page=1;line=12"
    field: str  # canonical field, e.g. "invoice_line.quantity"
    classification: str = "internal"

    def to_dict(self) -> dict[str, str]:
        return {
            "document_id": self.document_id,
            "version_id": self.version_id,
            "sha256": self.sha256,
            "locator": self.locator,
            "field": self.field,
            "classification": self.classification,
        }


Refs = dict[str, SourceRef]


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    name: str
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class POLine:
    line_number: str
    item_code: str | None
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    discount_amount: Decimal = Decimal(0)  # total discount on the line
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class PurchaseOrder:
    po_number: str
    supplier_id: str
    supplier_name: str
    currency: str
    lines: tuple[POLine, ...]
    status: Literal["open", "closed", "cancelled"] = "open"
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class InvoiceLine:
    line_number: str
    po_line_number: str | None
    item_code: str | None
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    discount_amount: Decimal = Decimal(0)
    kind: Literal["goods", "charge"] = "goods"
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class Invoice:
    invoice_number: str
    supplier_id: str | None
    supplier_name: str
    po_number: str | None
    currency: str
    invoice_date: date
    lines: tuple[InvoiceLine, ...]
    status: Literal["open", "cancelled"] = "open"
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class ReceiptLine:
    """One movement on a receiving record.

    `status` says what receiving decided about the units: `accepted`, `rejected` (delivered
    and refused at inspection), `reversed` (the posting was undone), or `unstated` (the source
    gives a received quantity without saying whether it was accepted; such units are
    delivered, never counted as accepted). `reason` is the source's rejection reason, if any.
    """

    po_line_number: str
    quantity: Decimal
    unit: str
    kind: Literal["receipt", "return"] = "receipt"
    status: Literal["accepted", "rejected", "reversed", "unstated"] = "accepted"
    reason: str | None = None
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class GoodsReceipt:
    receipt_number: str
    po_number: str
    received_date: date
    lines: tuple[ReceiptLine, ...]
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class InvoiceAllocation:
    """Quantity of a PO line already matched to another invoice in the source system."""

    invoice_number: str
    po_number: str
    po_line_number: str
    quantity: Decimal
    status: Literal["active", "cancelled"] = "active"
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class SupplierAssertion:
    """A claim found in supplier correspondence, e.g. "we delivered 20 chairs".

    Assertions are evidence about what the supplier says, not about what happened.
    """

    po_number: str
    po_line_number: str | None
    claim: Literal["delivered_quantity"]
    quantity: Decimal
    unit: str | None
    extraction_status: Literal["confirmed", "unconfirmed"] = "unconfirmed"
    refs: Refs = field(default_factory=dict)


ReceivingEventType = Literal[
    "rejection",
    "return",
    "replacement_promised",
    "replacement_delivery",
    "replacement_acceptance",
    "credit_promised",
    "credit_posted",
]


@dataclass(frozen=True)
class ReceivingEvent:
    """A receiving or disposition fact recorded after a delivery (docs/receiving-disposition.md).

    Each names what it concerns exactly, never by a guess:
    - `rejection`: units of a delivery refused at inspection after the receipt was posted;
      `receipt_number` is that receipt. Units it rejects stop counting as accepted.
    - `return`: rejected units sent back to the supplier.
    - `replacement_promised`, `replacement_delivery`, `replacement_acceptance`: the supplier's
      remedy by replacement; an acceptance follows the delivery it accepts.
    - `credit_promised`, `credit_posted`: the supplier's remedy by credit. A promise is not a
      credit; a posted credit names the credit document and the invoice it applies to.
    A disposition event refers to the rejected units through `follows` (the event it follows)
    or `receipt_number` (the receipt whose rejected lines it concerns).

    `occurred` is when it happened, as the source states it: `YYYY-MM-DD`, an RFC 3339 time
    with a zone, or None when the source does not say. When OpsBridge learned of it is the
    stored record's own time, kept apart.
    """

    event_id: str
    event_type: ReceivingEventType
    po_number: str
    po_line_number: str
    quantity: Decimal | None
    unit: str | None
    occurred: str | None = None
    receipt_number: str | None = None
    follows: str | None = None
    invoice_number: str | None = None
    document_number: str | None = None
    reason: str | None = None
    refs: Refs = field(default_factory=dict)


@dataclass(frozen=True)
class MatchInput:
    invoice: Invoice
    purchase_orders: tuple[PurchaseOrder, ...]
    receipts: tuple[GoodsReceipt, ...] = ()
    prior_allocations: tuple[InvoiceAllocation, ...] = ()
    supplier_assertions: tuple[SupplierAssertion, ...] = ()
    suppliers: tuple[Supplier, ...] = ()
    receiving_events: tuple[ReceivingEvent, ...] = ()
