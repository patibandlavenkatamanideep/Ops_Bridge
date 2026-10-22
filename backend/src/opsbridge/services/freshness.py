"""Whether a case's evidence is still what its latest investigation was computed from.

An investigation records the invoice, PO and supplier it matched on and a digest of the
evidence state, taken BEFORE matching reads anything: the versions matching would load now
(current records for that invoice and PO, whichever case ingested them), every version of
every document linked to the case (uploads included), and every correction on those
versions. A correction, an upload, or a new source version ingested for another case changes
the digest, and the result is stale until the investigation is re-run. A change that lands
while matching runs also makes it stale (a false alarm at worst, never a missed change).

This is local freshness only: it cannot show that the source system has not changed since
the records were fetched.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from opsbridge.canonical import digest
from opsbridge.evidence.records import load_match_input
from opsbridge.persistence.models import (
    CaseDocument,
    DocumentVersion,
    ExtractedField,
    FieldCorrection,
    InvestigationVersion,
)

STALE = (
    "evidence changed since the investigation (a correction, an upload or a new source "
    "version); re-run it"
)


def capture(
    s: Session,
    case_id: uuid.UUID,
    invoice_number: str | None,
    po_number: str | None,
    supplier_id: str | None,
) -> dict[str, Any]:
    used = (
        load_match_input(s, invoice_number, po_number, supplier_id).version_ids
        if invoice_number
        else set()
    )
    linked = select(CaseDocument.document_id).where(CaseDocument.case_id == case_id)
    versions = s.execute(
        select(DocumentVersion.id, DocumentVersion.raw_sha256, DocumentVersion.parse_status).where(
            or_(DocumentVersion.id.in_(used), DocumentVersion.document_id.in_(linked))
        )
    ).all()
    corrections = s.execute(
        select(FieldCorrection.id)
        .join(ExtractedField, ExtractedField.id == FieldCorrection.field_id)
        .where(ExtractedField.version_id.in_([v.id for v in versions]))
    ).scalars()
    state = {
        "versions": sorted([str(v.id), v.raw_sha256, v.parse_status] for v in versions),
        "corrections": sorted(str(c) for c in corrections),
    }
    return {
        "invoice_number": invoice_number,
        "po_number": po_number,
        "supplier_id": supplier_id,
        "digest": digest(state),
    }


def stale_reason(s: Session, case_id: uuid.UUID, iv: InvestigationVersion) -> str | None:
    """None when `iv` still describes the case's evidence; otherwise why not."""
    recorded = iv.evidence_state
    if not recorded:
        return "the investigation predates evidence fingerprints; re-run it"
    now = capture(
        s,
        case_id,
        recorded["invoice_number"],
        recorded["po_number"],
        recorded.get("supplier_id"),  # absent before suppliers were recorded
    )
    return None if now["digest"] == recorded["digest"] else STALE
