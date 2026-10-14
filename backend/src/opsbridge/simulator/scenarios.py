"""Synthetic scenario corpus → simulator records in each organization's native format.

Scenarios are hand-authored YAML with independently written expected labels. This
module renders them into the formats each simulated external system uses (furniture:
snake_case JSON, CSV receiving exports, text-layer PDF invoices; IT hardware: camelCase
JSON). Format differences live here, in the stand-in external systems, not in the
OpsBridge core, which reads them only through pack mappings.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .minipdf import image_pdf, text_pdf
from .store import SimStore


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupplierSpec(_M):
    id: str
    name: str


class POLineSpec(_M):
    line: int
    item: str | None = None
    desc: str
    qty: Decimal
    unit: str
    price: Decimal
    discount: Decimal = Decimal(0)


class POSpec(_M):
    number: str
    currency: str = "USD"
    status: Literal["open", "closed", "cancelled"] = "open"
    date: date
    lines: list[POLineSpec]


class ReceiptLineSpec(_M):
    line: int
    qty: Decimal
    unit: str = "EA"
    kind: Literal["receipt", "return"] = "receipt"
    status: Literal["accepted", "rejected", "reversed"] = "accepted"


class ReceiptSpec(_M):
    id: str
    date: date
    lines: list[ReceiptLineSpec]


class InvoiceLineSpec(_M):
    line: int
    po_line: int | None = None
    item: str | None = None
    desc: str
    qty: Decimal
    unit: str = "EA"
    price: Decimal
    discount: Decimal = Decimal(0)
    kind: Literal["goods", "charge"] = "goods"


class InvoiceSpec(_M):
    number: str
    date: date
    supplier_id: str | None
    supplier_name: str
    po_number: str | None
    currency: str = "USD"
    status: Literal["open", "cancelled"] = "open"
    lines: list[InvoiceLineSpec]
    # Document defects for extraction tests: amount_mismatch → line amounts inconsistent;
    # scanned → image-only PDF with no text layer.
    defect: Literal["none", "amount_mismatch", "scanned"] = "none"


class AllocationSpec(_M):
    invoice: str
    line: int
    qty: Decimal
    state: Literal["active", "cancelled"] = "active"


class MessageSpec(_M):
    id: str
    text: str


class ExpectedSpec(_M):
    outcome: Literal["matched", "discrepancies_found", "needs_information", "manual_review"]
    types: list[Literal["missing_receipt", "quantity_mismatch", "price_mismatch"]] = Field(
        default_factory=list
    )
    next_action: str
    note: str = ""


class Scenario(_M):
    id: str
    pack: Literal["furniture", "it_hardware"]
    family: str
    template: str
    split: Literal["dev", "validation", "heldout", "security"]
    description: str
    supplier: SupplierSpec
    po: POSpec | None
    receipts: list[ReceiptSpec] = Field(default_factory=list)
    invoice: InvoiceSpec
    allocations: list[AllocationSpec] = Field(default_factory=list)
    messages: list[MessageSpec] = Field(default_factory=list)
    expected: ExpectedSpec

    @property
    def case_title(self) -> str:
        return f"Invoice {self.invoice.number} blocked"


class Corpus(_M):
    schema_version: Literal[1] = 1
    corpus_id: str
    scenarios: list[Scenario]


def load_corpus(path: Path) -> Corpus:
    corpus = Corpus.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    ids = [s.id for s in corpus.scenarios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")
    return corpus


def _d(x: Decimal) -> str:
    return format(x, "f")


def _money(x: Decimal) -> str:
    return format(x.quantize(Decimal("0.01")), "f")


Record = tuple[str, str, bytes, str, str | None]  # collection, key, body, content type, po index


def render(s: Scenario) -> list[Record]:
    return _render_furniture(s) if s.pack == "furniture" else _render_it(s)


# --- furniture: snake_case JSON, CSV receipts, PDF invoices ---------------------------


def _render_furniture(s: Scenario) -> list[Record]:
    out: list[Record] = []
    doc: dict[str, Any]
    po_no = s.po.number if s.po else s.invoice.po_number
    if s.po:
        doc = {
            "po_number": s.po.number,
            "order_date": s.po.date.isoformat(),
            "supplier": {"id": s.supplier.id, "name": s.supplier.name},
            "currency": s.po.currency,
            "status": s.po.status.upper(),
            "lines": [
                {
                    "line_no": ln.line,
                    "sku": ln.item,
                    "description": ln.desc,
                    "qty_ordered": _d(ln.qty),
                    "uom": ln.unit,
                    "unit_price": _money(ln.price),
                    "discount": _money(ln.discount),
                }
                for ln in s.po.lines
            ],
        }
        out.append(
            (
                "purchase_orders",
                s.po.number,
                json.dumps(doc, indent=2).encode(),
                "application/json",
                s.po.number,
            )
        )
    out.append(("invoices", s.invoice.number, _furniture_invoice_pdf(s), "application/pdf", po_no))
    if s.receipts:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(
            ["receipt_id", "po_ref", "po_line", "received_on", "qty", "uom", "movement", "status"]
        )
        for r in s.receipts:
            for ln in r.lines:
                w.writerow(
                    [
                        r.id,
                        po_no,
                        ln.line,
                        r.date.isoformat(),
                        _d(ln.qty),
                        ln.unit,
                        ln.kind.upper(),
                        ln.status.upper(),
                    ]
                )
        out.append(("receipts", f"{po_no}-GR", buf.getvalue().encode(), "text/csv", po_no))
    if s.allocations:
        doc = {
            "po_number": po_no,
            "allocations": [
                {
                    "invoice": a.invoice,
                    "po": po_no,
                    "po_line": a.line,
                    "qty": _d(a.qty),
                    "state": "MATCHED" if a.state == "active" else "CANCELLED",
                }
                for a in s.allocations
            ],
        }
        out.append(
            (
                "allocations",
                f"{po_no}-ALLOC",
                json.dumps(doc, indent=2).encode(),
                "application/json",
                po_no,
            )
        )
    for m in s.messages:
        out.append(("messages", m.id, m.text.encode(), "text/plain", po_no))
    return out


def _furniture_invoice_pdf(s: Scenario) -> bytes:
    inv = s.invoice
    lines = [
        f"{inv.supplier_name.upper()} - INVOICE",
        f"Invoice No: {inv.number}",
        f"Invoice Date: {inv.date.isoformat()}",
        f"Supplier: {inv.supplier_name}",
    ]
    if inv.supplier_id:
        lines.append(f"Supplier ID: {inv.supplier_id}")
    if inv.po_number:
        lines.append(f"PO Reference: {inv.po_number}")
    lines += [
        f"Currency: {inv.currency}",
        "",
        "Line | PO Line | SKU | Description | Qty | UoM | Unit Price | Discount | Type | Amount",
    ]
    total = Decimal(0)
    for i, ln in enumerate(inv.lines):
        amount = ln.qty * ln.price - ln.discount
        if inv.defect == "amount_mismatch" and i == 0:
            amount += Decimal("10.00")
        total += amount
        lines.append(
            " | ".join(
                [
                    str(ln.line),
                    str(ln.po_line or ""),
                    ln.item or "",
                    ln.desc,
                    _d(ln.qty),
                    ln.unit,
                    _money(ln.price),
                    _money(ln.discount),
                    ln.kind.upper(),
                    _money(amount),
                ]
            )
        )
    lines += [
        "",
        f"Total: {_money(total)}",
        "Payment terms: Net 30. Synthetic document for testing.",
    ]
    if inv.defect == "scanned":
        return _scanned(lines)
    return text_pdf([lines])


def _scanned(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("L", (1240, 900), 255)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((40, 40 + i * 28), line, fill=0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return image_pdf(buf.getvalue(), img.width, img.height)


# --- IT hardware: camelCase JSON everywhere --------------------------------------------


def _render_it(s: Scenario) -> list[Record]:
    out: list[Record] = []
    doc: dict[str, Any]
    po_no = s.po.number if s.po else s.invoice.po_number
    if s.po:
        doc = {
            "purchaseOrderId": s.po.number,
            "orderedOn": s.po.date.isoformat(),
            "vendor": {"vendorCode": s.supplier.id, "legalName": s.supplier.name},
            "currencyCode": s.po.currency,
            "state": {"open": "Active", "closed": "Completed", "cancelled": "Voided"}[s.po.status],
            "items": [
                {
                    "lineId": str(ln.line),
                    "partNumber": ln.item,
                    "desc": ln.desc,
                    "quantity": _d(ln.qty),
                    "unitOfMeasure": ln.unit,
                    "unitCost": _money(ln.price),
                    "discountTotal": _money(ln.discount),
                }
                for ln in s.po.lines
            ],
        }
        out.append(
            (
                "purchase_orders",
                s.po.number,
                json.dumps(doc, indent=2).encode(),
                "application/json",
                s.po.number,
            )
        )
    inv = s.invoice
    inv_doc: dict[str, Any] = {
        "invoiceId": inv.number,
        "issuedOn": inv.date.isoformat(),
        "vendorName": inv.supplier_name,
        "currencyCode": inv.currency,
        "state": "Open" if inv.status == "open" else "Voided",
        "items": [
            {
                "lineId": str(ln.line),
                "poLineRef": str(ln.po_line) if ln.po_line is not None else None,
                "partNumber": ln.item,
                "desc": ln.desc,
                "quantity": _d(ln.qty),
                "unitOfMeasure": ln.unit,
                "unitCost": _money(ln.price),
                "discountTotal": _money(ln.discount),
                "lineType": "Hardware" if ln.kind == "goods" else "Shipping",
            }
            for ln in inv.lines
        ],
    }
    if inv.supplier_id:
        inv_doc["vendorCode"] = inv.supplier_id
    if inv.po_number:
        inv_doc["purchaseOrderRef"] = inv.po_number
    out.append(
        ("invoices", inv.number, json.dumps(inv_doc, indent=2).encode(), "application/json", po_no)
    )
    for r in s.receipts:
        doc = {
            "grnNumber": r.id,
            "poRef": po_no,
            "receivedDate": r.date.isoformat(),
            "items": [
                {
                    "poLineId": str(ln.line),
                    "qtyReceived": _d(ln.qty),
                    "uom": ln.unit,
                    "movementType": "Inbound" if ln.kind == "receipt" else "RMA",
                    "condition": ln.status.capitalize(),
                }
                for ln in r.lines
            ],
        }
        out.append(
            ("receipts", r.id, json.dumps(doc, indent=2).encode(), "application/json", po_no)
        )
    if s.allocations:
        doc = {
            "purchaseOrderId": po_no,
            "matches": [
                {
                    "invoiceId": a.invoice,
                    "purchaseOrderId": po_no,
                    "poLineId": str(a.line),
                    "matchedQty": _d(a.qty),
                    "matchState": "Matched" if a.state == "active" else "Reversed",
                }
                for a in s.allocations
            ],
        }
        out.append(
            (
                "allocations",
                f"{po_no}-MATCH",
                json.dumps(doc, indent=2).encode(),
                "application/json",
                po_no,
            )
        )
    for m in s.messages:
        out.append(("messages", m.id, m.text.encode(), "text/plain", po_no))
    return out


ORG_FOR_PACK = {"furniture": "demo-furniture", "it_hardware": "demo-it"}


def seed_simulator(
    store: SimStore, corpus: Corpus, org_for_pack: dict[str, str] | None = None
) -> int:
    orgs = org_for_pack or ORG_FOR_PACK
    count = 0
    for s in corpus.scenarios:
        for collection, key, body, ctype, po in render(s):
            store.put_record(orgs[s.pack], collection, key, body, ctype, po)
            count += 1
    return count
