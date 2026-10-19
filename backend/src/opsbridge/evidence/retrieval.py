"""Authorized lexical retrieval over evidence chunks (PostgreSQL full-text search).

The authorization filter (tenant via RLS + the caller's allowed document versions) is part
of the SQL WHERE clause, so ranking only ever sees permitted rows. This is lexical search;
it is not semantic or hybrid retrieval.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsbridge.persistence.models import DocumentVersion, EvidenceChunk, SourceDocument

RETRIEVAL_CONFIG = {
    "method": "postgres_fts",
    "dictionary": "english",
    "ranker": "ts_rank",
    "version": 1,
}


@dataclass(frozen=True)
class Hit:
    document_id: str
    version_id: str
    sha256: str
    locator: str
    title: str
    kind: str
    text: str
    rank: str  # fixed-precision text: canonical digests do not accept floats


def search(session: Session, version_ids: list[uuid.UUID], query: str, limit: int = 5) -> list[Hit]:
    if not version_ids or not query.strip():
        return []
    tsq = func.plainto_tsquery("english", query[:200])
    rank = func.ts_rank(EvidenceChunk.tsv, tsq)
    rows = session.execute(
        select(
            EvidenceChunk,
            DocumentVersion.document_id,
            DocumentVersion.raw_sha256,
            SourceDocument.title,
            SourceDocument.kind,
            rank.label("rank"),
        )
        .join(DocumentVersion, DocumentVersion.id == EvidenceChunk.version_id)
        .join(SourceDocument, SourceDocument.id == DocumentVersion.document_id)
        .where(EvidenceChunk.version_id.in_(version_ids), EvidenceChunk.tsv.op("@@")(tsq))
        .order_by(rank.desc(), EvidenceChunk.seq)
        .limit(max(1, min(limit, 20)))
    ).all()
    return [
        Hit(
            str(r[1]),
            str(r[0].version_id),
            r[2],
            r[0].locator,
            r[3],
            r[4],
            r[0].text_content[:600],
            f"{float(r[5]):.6f}",
        )
        for r in rows
    ]
