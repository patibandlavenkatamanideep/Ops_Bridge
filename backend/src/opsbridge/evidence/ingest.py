"""Evidence ingestion: raw bytes → document version → chunks, extracted fields, records.

Order of operations for one source document:
1. Size/type checks and bounded parsing (untrusted input).
2. Store raw bytes and their SHA-256 (before any normalization) in protected storage.
3. Create a new document version only when the raw digest changed.
4. Store normalized-text digest + normalization version separately.
5. Chunk text with page/line (or row) locators for retrieval.
6. Map to canonical records with the tenant's pack; persist extracted fields and records,
   superseding records derived from older versions of the same document.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from opsbridge.domain.records import SourceRef
from opsbridge.mapping import (
    DocRef,
    MappingResult,
    PackSpec,
    RecordType,
    map_document,
    map_ocr_candidates,
)

from ..persistence.models import (
    Artifact,
    DocumentVersion,
    EvidenceChunk,
    ExtractedField,
    GoodsReceiptRecord,
    InvoiceAllocationRecord,
    InvoiceLineRecord,
    InvoiceRecord,
    POLineRecord,
    PurchaseOrderRecord,
    ReceiptLineRecord,
    SourceDocument,
    SupplierAssertionRecord,
    SupplierRecord,
)
from .blobs import store_bytes
from .normalize import NORMALIZATION_VERSION, normalized_digest
from .parsers import MAX_BYTES, OcrSettings, ParsedDocument, parse, safe_filename
from .storage import ArtifactStore

CHUNK_LINES = 12
KIND_FOR_RECORD: dict[str, str] = {
    "purchase_order": "purchase_order",
    "invoice": "invoice",
    "goods_receipt": "goods_receipt",
    "allocation_history": "allocation_history",
    "supplier_message": "supplier_message",
}


class IngestRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class IngestResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    raw_sha256: str
    new_version: bool
    parse_status: str
    parse_detail: str | None
    problems: list[str] = field(default_factory=list)
    records: dict[str, int] = field(default_factory=dict)


def _refs_json(refs: dict[str, SourceRef]) -> dict[str, Any]:
    return {k: v.to_dict() for k, v in refs.items()}


def ingest(
    session: Session,
    store: ArtifactStore,
    *,
    tenant_id: uuid.UUID,
    pack: PackSpec | None,
    record_type: RecordType | None,
    source_key: str,
    origin: str,
    title: str,
    data: bytes,
    content_type: str,
    filename: str | None = None,
    created_by: uuid.UUID | None = None,
    classification: str = "internal",
    ocr: OcrSettings | None = None,
) -> IngestResult:
    """Ingest one document inside the caller's tenant-scoped transaction."""
    if len(data) > MAX_BYTES:
        raise IngestRejected(f"file exceeds {MAX_BYTES} bytes")
    parsed = parse(data, content_type, ocr)
    if parsed.status == "rejected" and parsed.parser == "none":
        # Unsupported or mismatched type: nothing is stored.
        raise IngestRejected(parsed.detail or "unsupported document")

    kind = KIND_FOR_RECORD.get(record_type or "", "attachment")
    doc = session.execute(
        select(SourceDocument).where(SourceDocument.source_key == source_key)
    ).scalar_one_or_none()
    if doc is None:
        doc = SourceDocument(
            tenant_id=tenant_id,
            source_key=source_key,
            kind=kind,
            origin=origin,
            title=title[:300],
            classification=classification,
            created_by=created_by,
        )
        session.add(doc)
        session.flush()

    storage_key, raw_sha = store_bytes(session, store, tenant_id, data)
    latest = session.execute(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == doc.id)
        .order_by(DocumentVersion.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None and latest.raw_sha256 == raw_sha:
        return IngestResult(
            doc.id,
            latest.id,
            latest.version_no,
            raw_sha,
            False,
            latest.parse_status,
            latest.parse_detail,
        )

    artifact = Artifact(
        tenant_id=tenant_id,
        kind="raw_document",
        sha256=raw_sha,
        size_bytes=len(data),
        content_type=parsed.content_type,
        storage_key=storage_key,
        classification=classification,
    )
    session.add(artifact)
    session.flush()

    mapping: MappingResult | None = None
    status = parsed.status
    detail = parsed.detail
    version = DocumentVersion(
        tenant_id=tenant_id,
        document_id=doc.id,
        version_no=(latest.version_no + 1) if latest else 1,
        raw_artifact_id=artifact.id,
        raw_sha256=raw_sha,
        raw_size=len(data),
        content_type=parsed.content_type,
        safe_filename=safe_filename(filename),
        page_count=parsed.page_count,
        parser=parsed.parser,
        normalized_sha256=normalized_digest(parsed.text) if parsed.text else None,
        normalization_version=NORMALIZATION_VERSION if parsed.text else None,
        parse_status=status,
        parse_detail=detail,
        created_by=created_by,
    )
    session.add(version)
    session.flush()

    _store_chunks(session, tenant_id, version.id, parsed)

    result = IngestResult(doc.id, version.id, version.version_no, raw_sha, True, status, detail)
    if parsed.flags:
        result.problems.extend(f"flag:{f}" for f in parsed.flags)
    if pack is not None and record_type is not None and status != "parsed":
        # The newest version of this source yields no records, so the facts extracted from an
        # earlier version must stop being current: matching would otherwise use what the
        # source no longer shows. They stay in the history, superseded.
        _supersede_all(session, doc.id, version.id)
    if pack is not None and record_type is not None and status == "parsed":
        mapping = map_document(
            pack, record_type, parsed, DocRef(str(doc.id), str(version.id), raw_sha, classification)
        )
        _store_fields(session, tenant_id, version.id, mapping)
        result.records = _store_records(session, tenant_id, doc.id, version.id, mapping)
        if mapping.problems:
            version.parse_status = "needs_review"
            version.parse_detail = "; ".join(mapping.problems)[:2000]
            result.parse_status = "needs_review"
            result.parse_detail = version.parse_detail
            result.problems.extend(mapping.problems)
    elif pack is not None and record_type is not None and parsed.ocr_engine:
        # OCR text: candidate values for review only; no records, so no matching input.
        candidates = map_ocr_candidates(
            pack, record_type, parsed, DocRef(str(doc.id), str(version.id), raw_sha, classification)
        )
        _store_fields(session, tenant_id, version.id, candidates)
        result.problems.extend(f"ocr: {p}" for p in candidates.problems)
    return result


def _store_chunks(
    session: Session, tenant_id: uuid.UUID, version_id: uuid.UUID, parsed: ParsedDocument
) -> None:
    if parsed.data is not None and parsed.csv_header is not None:
        # CSV: header + one chunk per data row, with the row number as locator.
        header = ",".join(parsed.csv_header)
        for i, row in enumerate(parsed.data):
            text = header + "\n" + ",".join(row.values())
            session.add(
                EvidenceChunk(
                    tenant_id=tenant_id,
                    version_id=version_id,
                    seq=i,
                    locator=f"csv:row={i + 2}",
                    page=None,
                    line_start=i + 2,
                    line_end=i + 2,
                    text_content=text,
                )
            )
        return
    seq = 0
    by_page: dict[int | None, list[Any]] = {}
    for tl in parsed.lines:
        by_page.setdefault(tl.page, []).append(tl)
    for page, lines in by_page.items():
        for start in range(0, len(lines), CHUNK_LINES):
            part = lines[start : start + CHUNK_LINES]
            first, last = part[0].line, part[-1].line
            prefix = "ocr" if part[0].origin == "ocr" else "pdf"
            locator = (
                f"{prefix}:page={page};lines={first}-{last}"
                if page is not None
                else f"text:lines={first}-{last}"
            )
            session.add(
                EvidenceChunk(
                    tenant_id=tenant_id,
                    version_id=version_id,
                    seq=seq,
                    locator=locator,
                    page=page,
                    line_start=first,
                    line_end=last,
                    text_content="\n".join(t.text for t in part),
                )
            )
            seq += 1


def _store_fields(
    session: Session, tenant_id: uuid.UUID, version_id: uuid.UUID, mapping: MappingResult
) -> None:
    for fv in mapping.fields:
        session.add(
            ExtractedField(
                tenant_id=tenant_id,
                version_id=version_id,
                record_type=fv.record_type,
                record_key=fv.record_key[:200],
                field_path=fv.field_path,
                value_text=fv.value,
                value_type=fv.value_type,
                locator=fv.locator[:200],
                method=fv.method,
                status=fv.status,
                note=fv.note,
            )
        )


def _supersede(
    session: Session, model: Any, document_id: uuid.UUID, keep_version: uuid.UUID
) -> None:
    older = select(DocumentVersion.id).where(
        DocumentVersion.document_id == document_id, DocumentVersion.id != keep_version
    )
    session.execute(
        update(model)
        .where(model.source_version_id.in_(older), model.superseded_at.is_(None))
        .values(superseded_at=datetime.now(UTC))
    )


def _supersede_all(session: Session, document_id: uuid.UUID, keep_version: uuid.UUID) -> None:
    for model in (
        PurchaseOrderRecord,
        InvoiceRecord,
        GoodsReceiptRecord,
        InvoiceAllocationRecord,
        SupplierAssertionRecord,
    ):
        _supersede(session, model, document_id, keep_version)
    session.flush()


def _store_records(
    session: Session,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    m: MappingResult,
) -> dict[str, int]:
    _supersede_all(session, document_id, version_id)
    for po in m.purchase_orders:
        # A PO number can only have one current record per tenant; a newer document wins.
        session.execute(
            update(PurchaseOrderRecord)
            .where(
                PurchaseOrderRecord.po_number == po.po_number,
                PurchaseOrderRecord.superseded_at.is_(None),
            )
            .values(superseded_at=datetime.now(UTC))
        )
        row = PurchaseOrderRecord(
            tenant_id=tenant_id,
            source_version_id=version_id,
            po_number=po.po_number,
            supplier_key=po.supplier_id,
            supplier_name=po.supplier_name,
            currency=po.currency,
            status=po.status,
            refs=_refs_json(po.refs),
        )
        session.add(row)
        session.flush()
        for ln in po.lines:
            session.add(
                POLineRecord(
                    tenant_id=tenant_id,
                    purchase_order_id=row.id,
                    line_number=ln.line_number,
                    item_code=ln.item_code,
                    description=ln.description,
                    quantity=ln.quantity,
                    unit=ln.unit,
                    unit_price=ln.unit_price,
                    discount_amount=ln.discount_amount,
                    refs=_refs_json(ln.refs),
                )
            )
        exists = session.execute(
            select(func.count())
            .select_from(SupplierRecord)
            .where(SupplierRecord.supplier_key == po.supplier_id)
        ).scalar_one()
        if not exists:
            session.add(
                SupplierRecord(
                    tenant_id=tenant_id, supplier_key=po.supplier_id, name=po.supplier_name
                )
            )
    for inv in m.invoices:
        # A newer document wins for the same supplier's invoice; another supplier's invoice
        # with the same number is a different invoice and stays current.
        session.execute(
            update(InvoiceRecord)
            .where(
                InvoiceRecord.invoice_number == inv.invoice_number,
                InvoiceRecord.supplier_key.is_not_distinct_from(inv.supplier_id),
                InvoiceRecord.superseded_at.is_(None),
            )
            .values(superseded_at=datetime.now(UTC))
        )
        irow = InvoiceRecord(
            tenant_id=tenant_id,
            source_version_id=version_id,
            invoice_number=inv.invoice_number,
            supplier_key=inv.supplier_id,
            supplier_name=inv.supplier_name,
            po_number=inv.po_number,
            currency=inv.currency,
            invoice_date=inv.invoice_date,
            status=inv.status,
            refs=_refs_json(inv.refs),
        )
        session.add(irow)
        session.flush()
        for il in inv.lines:
            session.add(
                InvoiceLineRecord(
                    tenant_id=tenant_id,
                    invoice_id=irow.id,
                    line_number=il.line_number,
                    po_line_number=il.po_line_number,
                    item_code=il.item_code,
                    description=il.description,
                    quantity=il.quantity,
                    unit=il.unit,
                    unit_price=il.unit_price,
                    discount_amount=il.discount_amount,
                    kind=il.kind,
                    refs=_refs_json(il.refs),
                )
            )
    for gr in m.receipts:
        session.execute(
            update(GoodsReceiptRecord)
            .where(
                GoodsReceiptRecord.receipt_number == gr.receipt_number,
                GoodsReceiptRecord.superseded_at.is_(None),
            )
            .values(superseded_at=datetime.now(UTC))
        )
        grow = GoodsReceiptRecord(
            tenant_id=tenant_id,
            source_version_id=version_id,
            receipt_number=gr.receipt_number,
            po_number=gr.po_number,
            received_date=gr.received_date,
            refs=_refs_json(gr.refs),
        )
        session.add(grow)
        session.flush()
        for rl in gr.lines:
            session.add(
                ReceiptLineRecord(
                    tenant_id=tenant_id,
                    goods_receipt_id=grow.id,
                    po_line_number=rl.po_line_number,
                    quantity=rl.quantity,
                    unit=rl.unit,
                    kind=rl.kind,
                    status=rl.status,
                    refs=_refs_json(rl.refs),
                )
            )
    for a in m.allocations:
        session.add(
            InvoiceAllocationRecord(
                tenant_id=tenant_id,
                source_version_id=version_id,
                invoice_number=a.invoice_number,
                po_number=a.po_number,
                po_line_number=a.po_line_number,
                quantity=a.quantity,
                status=a.status,
                refs=_refs_json(a.refs),
            )
        )
    for sa in m.assertions:
        session.add(
            SupplierAssertionRecord(
                tenant_id=tenant_id,
                source_version_id=version_id,
                po_number=sa.po_number,
                po_line_number=sa.po_line_number,
                claim=sa.claim,
                quantity=sa.quantity,
                unit=sa.unit,
                extraction_status=sa.extraction_status,
                refs=_refs_json(sa.refs),
            )
        )
    return {
        "purchase_orders": len(m.purchase_orders),
        "invoices": len(m.invoices),
        "goods_receipts": len(m.receipts),
        "allocations": len(m.allocations),
        "supplier_assertions": len(m.assertions),
    }
