"""Citation resolution and validation against the exact source version.

A citation is valid only if (a) the document version exists in the caller's tenant
(RLS-scoped query), (b) its raw digest equals the cited digest, and (c) the locator
resolves to an extracted field or a text chunk of that version. Validation shows that
the cited location exists and what it says; whether it supports a claim is checked
separately by comparing the resolved value with the claimed value.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from opsbridge.domain.records import SourceRef
from opsbridge.persistence.models import (
    DocumentVersion,
    EvidenceChunk,
    ExtractedField,
    SourceDocument,
)

CitationStatus = Literal["valid", "not_found", "digest_mismatch", "locator_unresolved"]
_LINE = re.compile(r"(?:page=(?P<page>\d+);)?line=(?P<line>\d+)")
_ROW = re.compile(r"csv:row=(?P<row>\d+)")


@dataclass(frozen=True)
class ResolvedCitation:
    ref: SourceRef
    status: CitationStatus
    document_title: str | None = None
    version_no: int | None = None
    value: str | None = None
    field_status: str | None = None
    excerpt: str | None = None

    def to_dict(self, include_excerpt: bool) -> dict[str, object]:
        d: dict[str, object] = {
            **self.ref.to_dict(),
            "status": self.status,
            "document_title": self.document_title,
            "version_no": self.version_no,
            "value": self.value,
            "field_status": self.field_status,
        }
        if include_excerpt:
            d["excerpt"] = self.excerpt
        return d


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def resolve(session: Session, ref: SourceRef) -> ResolvedCitation:
    vid = _parse_uuid(ref.version_id)
    did = _parse_uuid(ref.document_id)
    if vid is None or did is None:
        return ResolvedCitation(ref, "not_found")
    row = session.execute(
        select(DocumentVersion, SourceDocument.title)
        .join(SourceDocument, SourceDocument.id == DocumentVersion.document_id)
        .where(DocumentVersion.id == vid, DocumentVersion.document_id == did)
    ).first()
    if row is None:
        return ResolvedCitation(ref, "not_found")
    version, title = row
    if version.raw_sha256 != ref.sha256:
        return ResolvedCitation(ref, "digest_mismatch", title, version.version_no)
    fld = (
        session.execute(
            select(ExtractedField).where(
                ExtractedField.version_id == vid,
                ExtractedField.locator == ref.locator,
                ExtractedField.field_path == ref.field,
            )
        )
        .scalars()
        .first()
    )
    excerpt = _excerpt(session, vid, ref.locator)
    if fld is None and excerpt is None:
        return ResolvedCitation(ref, "locator_unresolved", title, version.version_no)
    return ResolvedCitation(
        ref,
        "valid",
        title,
        version.version_no,
        value=fld.value_text if fld else None,
        field_status=fld.status if fld else None,
        excerpt=excerpt,
    )


def _excerpt(session: Session, version_id: uuid.UUID, locator: str) -> str | None:
    m = _ROW.search(locator)
    if m:
        chunk = (
            session.execute(
                select(EvidenceChunk).where(
                    EvidenceChunk.version_id == version_id,
                    EvidenceChunk.locator == f"csv:row={m.group('row')}",
                )
            )
            .scalars()
            .first()
        )
        return chunk.text_content if chunk else None
    m = _LINE.search(locator)
    if m:
        line = int(m.group("line"))
        page = int(m.group("page")) if m.group("page") else None
        q = select(EvidenceChunk).where(
            EvidenceChunk.version_id == version_id,
            EvidenceChunk.line_start <= line,
            EvidenceChunk.line_end >= line,
        )
        q = (
            q.where(EvidenceChunk.page == page)
            if page is not None
            else q.where(EvidenceChunk.page.is_(None))
        )
        chunk = session.execute(q).scalars().first()
        if chunk is None:
            return None
        lines = chunk.text_content.split("\n")
        idx = line - (chunk.line_start or line)
        return lines[idx] if 0 <= idx < len(lines) else None
    if locator.startswith("json:"):
        # JSON values are cited by pointer; the extracted field carries the value.
        return None
    return None


def resolve_all(session: Session, refs: list[SourceRef]) -> list[ResolvedCitation]:
    seen: set[tuple[str, str, str]] = set()
    out = []
    for ref in refs:
        key = (ref.version_id, ref.locator, ref.field)
        if key in seen:
            continue
        seen.add(key)
        out.append(resolve(session, ref))
    return out


def chunk_search_filter(version_ids: list[uuid.UUID]):  # type: ignore[no-untyped-def]
    """Restrict retrieval to authorized versions BEFORE ranking (used by retrieval)."""
    return and_(EvidenceChunk.version_id.in_(version_ids)) if version_ids else or_(False)
