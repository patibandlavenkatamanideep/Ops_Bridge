"""Build matcher inputs from current (non-superseded) records, with human corrections.

Corrections never overwrite the original extraction. They are stored separately and
applied here as an overlay keyed by (document version, locator, canonical field), which
is exactly how each record field's SourceRef identifies its origin.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsbridge.domain.money import DecimalParseError, parse_decimal
from opsbridge.domain.records import (
    GoodsReceipt,
    Invoice,
    InvoiceAllocation,
    InvoiceLine,
    MatchInput,
    POLine,
    PurchaseOrder,
    ReceiptLine,
    SourceRef,
    Supplier,
    SupplierAssertion,
)
from opsbridge.persistence.models import (
    DocumentVersion,
    ExtractedField,
    FieldCorrection,
    GoodsReceiptRecord,
    InvoiceAllocationRecord,
    InvoiceLineRecord,
    InvoiceRecord,
    POLineRecord,
    PurchaseOrderRecord,
    ReceiptLineRecord,
    SupplierAssertionRecord,
    SupplierRecord,
)

Overlay = dict[tuple[str, str, str], tuple[str, str]]  # key → (corrected value, correction id)


def _refs(raw: dict[str, Any] | None) -> dict[str, SourceRef]:
    return {k: SourceRef(**v) for k, v in (raw or {}).items()}


def case_identifiers(session: Session, case_id: uuid.UUID) -> list[str]:
    """The case's own business identifiers (case, invoice and PO numbers), which outbound
    redaction keeps even when they look like phone numbers."""
    from opsbridge.persistence.models import Case

    case = session.get(Case, case_id)
    if case is None:
        return []
    values = {case.case_number, case.invoice_number, case.po_number}
    if case.invoice_number:
        values.update(
            session.execute(
                select(InvoiceRecord.po_number).where(
                    InvoiceRecord.invoice_number == case.invoice_number,
                    InvoiceRecord.superseded_at.is_(None),
                )
            ).scalars()
        )
    return sorted(v for v in values if v)


def load_corrections(session: Session, version_ids: set[uuid.UUID]) -> Overlay:
    """Latest human correction per extracted field (the original extraction is untouched)."""
    if not version_ids:
        return {}
    rows = session.execute(
        select(ExtractedField, FieldCorrection)
        .join(FieldCorrection, FieldCorrection.field_id == ExtractedField.id)
        .where(ExtractedField.version_id.in_(version_ids))
        .order_by(FieldCorrection.created_at)
    ).all()
    overlay: Overlay = {}
    for fld, corr in rows:  # later corrections overwrite earlier ones
        overlay[(str(fld.version_id), fld.locator, fld.field_path)] = (
            corr.new_value or "",
            str(corr.id),
        )
    return overlay


# The fields whose human corrections matching applies (below, in load_match_input). Any other
# field is refused at correction time: a correction must change what the next run uses.
CORRECTABLE_FIELDS = frozenset(
    {
        "invoice.supplier_id",
        "invoice_line.po_line_number",
        "invoice_line.quantity",
        "invoice_line.unit_price",
        "po_line.quantity",
        "po_line.unit_price",
        "receipt_line.quantity",
    }
)


@dataclass
class _Applier:
    overlay: Overlay
    applied: list[dict[str, str]] = field(default_factory=list)

    def dec(self, value: Decimal, refs: dict[str, SourceRef], name: str) -> Decimal:
        assert name in CORRECTABLE_FIELDS, name
        ref = refs.get(name)
        if ref is None:
            return value
        hit = self.overlay.get((ref.version_id, ref.locator, name))
        if hit is None:
            return value
        corrected, correction_id = hit
        try:
            new = parse_decimal(corrected)
        except DecimalParseError:
            return value
        self.applied.append(
            {
                "field": name,
                "version_id": ref.version_id,
                "locator": ref.locator,
                "original": format(value, "f"),
                "corrected": corrected,
                "correction_id": correction_id,
            }
        )
        return new

    def text(self, value: str | None, refs: dict[str, SourceRef], name: str) -> str | None:
        assert name in CORRECTABLE_FIELDS, name
        ref = refs.get(name)
        if ref is None:
            return value
        hit = self.overlay.get((ref.version_id, ref.locator, name))
        if hit is None:
            return value
        corrected, correction_id = hit
        self.applied.append(
            {
                "field": name,
                "version_id": ref.version_id,
                "locator": ref.locator,
                "original": value or "",
                "corrected": corrected,
                "correction_id": correction_id,
            }
        )
        return corrected


@dataclass
class LoadedEvidence:
    match_input: MatchInput | None
    version_ids: set[uuid.UUID]
    corrections_applied: list[dict[str, str]]
    missing: list[str]


def load_match_input(session: Session, invoice_number: str, po_hint: str | None) -> LoadedEvidence:
    missing: list[str] = []
    inv_row = session.execute(
        select(InvoiceRecord).where(
            InvoiceRecord.invoice_number == invoice_number, InvoiceRecord.superseded_at.is_(None)
        )
    ).scalar_one_or_none()
    if inv_row is None:
        return LoadedEvidence(None, set(), [], [f"invoice {invoice_number}"])
    versions: set[uuid.UUID] = {inv_row.source_version_id}
    po_number = inv_row.po_number or po_hint
    po_rows = []
    receipts_rows = []
    alloc_rows = []
    assertion_rows = []
    if po_number:
        po_rows = list(
            session.execute(
                select(PurchaseOrderRecord).where(
                    PurchaseOrderRecord.po_number == po_number,
                    PurchaseOrderRecord.superseded_at.is_(None),
                )
            ).scalars()
        )
        receipts_rows = list(
            session.execute(
                select(GoodsReceiptRecord).where(
                    GoodsReceiptRecord.po_number == po_number,
                    GoodsReceiptRecord.superseded_at.is_(None),
                )
            ).scalars()
        )
        alloc_rows = list(
            session.execute(
                select(InvoiceAllocationRecord).where(
                    InvoiceAllocationRecord.po_number == po_number,
                    InvoiceAllocationRecord.superseded_at.is_(None),
                )
            ).scalars()
        )
        assertion_rows = list(
            session.execute(
                select(SupplierAssertionRecord).where(
                    SupplierAssertionRecord.po_number == po_number,
                    SupplierAssertionRecord.superseded_at.is_(None),
                )
            ).scalars()
        )
        if not po_rows:
            missing.append(f"purchase order {po_number}")
    for rows in (po_rows, receipts_rows, alloc_rows, assertion_rows):
        versions |= {r.source_version_id for r in rows}

    ap = _Applier(load_corrections(session, versions))

    inv_lines = session.execute(
        select(InvoiceLineRecord)
        .where(InvoiceLineRecord.invoice_id == inv_row.id)
        .order_by(InvoiceLineRecord.line_number)
    ).scalars()
    invoice = Invoice(
        invoice_number=inv_row.invoice_number,
        supplier_id=ap.text(inv_row.supplier_key, _refs(inv_row.refs), "invoice.supplier_id"),
        supplier_name=inv_row.supplier_name,
        po_number=inv_row.po_number,
        currency=inv_row.currency,
        invoice_date=inv_row.invoice_date,
        status=cast(Any, inv_row.status),
        refs=_refs(inv_row.refs),
        lines=tuple(
            InvoiceLine(
                line_number=ln.line_number,
                po_line_number=ap.text(
                    ln.po_line_number, _refs(ln.refs), "invoice_line.po_line_number"
                ),
                item_code=ln.item_code,
                description=ln.description,
                quantity=ap.dec(ln.quantity, _refs(ln.refs), "invoice_line.quantity"),
                unit=ln.unit,
                unit_price=ap.dec(ln.unit_price, _refs(ln.refs), "invoice_line.unit_price"),
                discount_amount=ln.discount_amount,
                kind=cast(Any, ln.kind),
                refs=_refs(ln.refs),
            )
            for ln in inv_lines
        ),
    )
    pos = []
    for po in po_rows:
        po_lines = session.execute(
            select(POLineRecord)
            .where(POLineRecord.purchase_order_id == po.id)
            .order_by(POLineRecord.line_number)
        ).scalars()
        pos.append(
            PurchaseOrder(
                po_number=po.po_number,
                supplier_id=po.supplier_key,
                supplier_name=po.supplier_name,
                currency=po.currency,
                status=cast(Any, po.status),
                refs=_refs(po.refs),
                lines=tuple(
                    POLine(
                        line_number=ln.line_number,
                        item_code=ln.item_code,
                        description=ln.description,
                        quantity=ap.dec(ln.quantity, _refs(ln.refs), "po_line.quantity"),
                        unit=ln.unit,
                        unit_price=ap.dec(ln.unit_price, _refs(ln.refs), "po_line.unit_price"),
                        discount_amount=ln.discount_amount,
                        refs=_refs(ln.refs),
                    )
                    for ln in po_lines
                ),
            )
        )
    receipts = []
    for gr in receipts_rows:
        r_lines = session.execute(
            select(ReceiptLineRecord).where(ReceiptLineRecord.goods_receipt_id == gr.id)
        ).scalars()
        receipts.append(
            GoodsReceipt(
                receipt_number=gr.receipt_number,
                po_number=gr.po_number,
                received_date=gr.received_date,
                refs=_refs(gr.refs),
                lines=tuple(
                    ReceiptLine(
                        po_line_number=rl.po_line_number,
                        quantity=ap.dec(rl.quantity, _refs(rl.refs), "receipt_line.quantity"),
                        unit=rl.unit,
                        kind=cast(Any, rl.kind),
                        status=cast(Any, rl.status),
                        refs=_refs(rl.refs),
                    )
                    for rl in r_lines
                ),
            )
        )
    allocations = tuple(
        InvoiceAllocation(
            invoice_number=a.invoice_number,
            po_number=a.po_number,
            po_line_number=a.po_line_number,
            quantity=a.quantity,
            status=cast(Any, a.status),
            refs=_refs(a.refs),
        )
        for a in alloc_rows
    )
    assertions = tuple(
        SupplierAssertion(
            po_number=a.po_number,
            po_line_number=a.po_line_number,
            claim="delivered_quantity",
            quantity=a.quantity,
            unit=a.unit,
            extraction_status=cast(Any, a.extraction_status),
            refs=_refs(a.refs),
        )
        for a in assertion_rows
    )
    suppliers = tuple(
        Supplier(s.supplier_key, s.name) for s in session.execute(select(SupplierRecord)).scalars()
    )
    return LoadedEvidence(
        MatchInput(
            invoice=invoice,
            purchase_orders=tuple(pos),
            receipts=tuple(receipts),
            prior_allocations=allocations,
            supplier_assertions=assertions,
            suppliers=suppliers,
        ),
        versions,
        ap.applied,
        missing,
    )


def source_manifest(session: Session, version_ids: set[uuid.UUID]) -> list[dict[str, Any]]:
    if not version_ids:
        return []
    rows = session.execute(
        select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
    ).scalars()
    return sorted(
        (
            {
                "document_id": str(v.document_id),
                "version_id": str(v.id),
                "version_no": v.version_no,
                "raw_sha256": v.raw_sha256,
                "normalized_sha256": v.normalized_sha256,
                "normalization_version": v.normalization_version,
                "parse_status": v.parse_status,
            }
            for v in rows
        ),
        key=lambda d: d["version_id"],
    )
