"""Hand-written record builders for domain tests.

These are independent of any parser or pack mapping: values are typed directly so the
matcher is checked against hand-computed expectations, not against its own output.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.domain.records import (
    GoodsReceipt,
    Invoice,
    InvoiceAllocation,
    InvoiceLine,
    POLine,
    PurchaseOrder,
    ReceiptLine,
    SourceRef,
    SupplierAssertion,
)

PACKS = Path(__file__).resolve().parents[3] / "packs"


def load_policy(rel: str) -> ReconciliationPolicy:
    return ReconciliationPolicy.model_validate(yaml.safe_load((PACKS / rel).read_text()))


def ref(doc: str, field: str, locator: str = "json:/") -> SourceRef:
    return SourceRef(
        document_id=doc,
        version_id=f"{doc}-v1",
        sha256="sha256:" + "0" * 64,
        locator=locator,
        field=field,
    )


def D(x: str) -> Decimal:
    return Decimal(x)


def po(
    number: str = "PO-1001",
    supplier_id: str = "SUP-ACME",
    supplier_name: str = "Acme Office Furniture Inc",
    currency: str = "USD",
    lines: tuple[POLine, ...] | None = None,
    status: str = "open",
) -> PurchaseOrder:
    if lines is None:
        lines = (po_line("1", "20", "185.00"),)
    return PurchaseOrder(
        po_number=number,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        currency=currency,
        lines=lines,
        status=status,  # type: ignore[arg-type]
        refs={
            "purchase_order.supplier_id": ref(number, "purchase_order.supplier_id"),
            "purchase_order.supplier_name": ref(number, "purchase_order.supplier_name"),
            "purchase_order.currency": ref(number, "purchase_order.currency"),
        },
    )


def po_line(
    number: str,
    qty: str,
    price: str,
    unit: str = "EA",
    item: str | None = None,
    discount: str = "0",
) -> POLine:
    doc = "PO-1001"
    return POLine(
        line_number=number,
        item_code=item or f"ITEM-{number}",
        description=f"Item {number}",
        quantity=D(qty),
        unit=unit,
        unit_price=D(price),
        discount_amount=D(discount),
        refs={
            "po_line.quantity": ref(doc, "po_line.quantity", f"json:/lines/{number}/quantity"),
            "po_line.unit_price": ref(
                doc, "po_line.unit_price", f"json:/lines/{number}/unit_price"
            ),
            "po_line.unit": ref(doc, "po_line.unit", f"json:/lines/{number}/unit"),
            "po_line.item_code": ref(doc, "po_line.item_code", f"json:/lines/{number}/item"),
        },
    )


def inv_line(
    number: str,
    po_line_number: str | None,
    qty: str,
    price: str,
    unit: str = "EA",
    item: str | None = None,
    kind: str = "goods",
    discount: str = "0",
) -> InvoiceLine:
    doc = "INV-9001"
    return InvoiceLine(
        line_number=number,
        po_line_number=po_line_number,
        item_code=item,
        description=f"Invoice line {number}",
        quantity=D(qty),
        unit=unit,
        unit_price=D(price),
        discount_amount=D(discount),
        kind=kind,  # type: ignore[arg-type]
        refs={
            "invoice_line.quantity": ref(
                doc, "invoice_line.quantity", f"pdf:page=1;line={10 + int(number)}"
            ),
            "invoice_line.unit_price": ref(
                doc, "invoice_line.unit_price", f"pdf:page=1;line={10 + int(number)}"
            ),
            "invoice_line.unit": ref(doc, "invoice_line.unit"),
            "invoice_line.po_line_number": ref(doc, "invoice_line.po_line_number"),
            "invoice_line.item_code": ref(doc, "invoice_line.item_code"),
        },
    )


def invoice(
    lines: tuple[InvoiceLine, ...],
    number: str = "INV-9001",
    po_number: str | None = "PO-1001",
    supplier_id: str | None = "SUP-ACME",
    supplier_name: str = "Acme Office Furniture Inc",
    currency: str = "USD",
) -> Invoice:
    return Invoice(
        invoice_number=number,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        po_number=po_number,
        currency=currency,
        invoice_date=date(2026, 9, 30),
        lines=lines,
        refs={
            "invoice.po_number": ref(number, "invoice.po_number"),
            "invoice.supplier_id": ref(number, "invoice.supplier_id"),
            "invoice.supplier_name": ref(number, "invoice.supplier_name"),
            "invoice.currency": ref(number, "invoice.currency"),
        },
    )


def receipt(
    lines: tuple[ReceiptLine, ...], number: str = "GR-5001", po_number: str = "PO-1001"
) -> GoodsReceipt:
    return GoodsReceipt(
        receipt_number=number, po_number=po_number, received_date=date(2026, 9, 20), lines=lines
    )


def r_line(
    po_line_number: str,
    qty: str,
    unit: str = "EA",
    kind: str = "receipt",
    status: str = "accepted",
    doc: str = "GR-5001",
) -> ReceiptLine:
    return ReceiptLine(
        po_line_number=po_line_number,
        quantity=D(qty),
        unit=unit,
        kind=kind,
        status=status,  # type: ignore[arg-type]
        refs={
            "receipt_line.quantity": ref(
                doc, "receipt_line.quantity", f"csv:row=2;col=qty_accepted;line={po_line_number}"
            ),
            "receipt_line.unit": ref(doc, "receipt_line.unit"),
        },
    )


def allocation(
    invoice_number: str, po_line_number: str, qty: str, status: str = "active"
) -> InvoiceAllocation:
    return InvoiceAllocation(
        invoice_number=invoice_number,
        po_number="PO-1001",
        po_line_number=po_line_number,
        quantity=D(qty),
        status=status,  # type: ignore[arg-type]
        refs={"allocation.quantity": ref(invoice_number, "allocation.quantity")},
    )


def assertion(qty: str, po_line_number: str | None = "1") -> SupplierAssertion:
    return SupplierAssertion(
        po_number="PO-1001",
        po_line_number=po_line_number,
        claim="delivered_quantity",
        quantity=D(qty),
        unit="EA",
        refs={
            "supplier_message.claim": ref(
                "MSG-77", "supplier_message.delivered_quantity", "text:line=4"
            )
        },
    )
