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
    po_line_number: str
    quantity: Decimal
    unit: str
    kind: Literal["receipt", "return"] = "receipt"
    status: Literal["accepted", "rejected", "reversed"] = "accepted"
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


@dataclass(frozen=True)
class MatchInput:
    invoice: Invoice
    purchase_orders: tuple[PurchaseOrder, ...]
    receipts: tuple[GoodsReceipt, ...] = ()
    prior_allocations: tuple[InvoiceAllocation, ...] = ()
    supplier_assertions: tuple[SupplierAssertion, ...] = ()
    suppliers: tuple[Supplier, ...] = ()
