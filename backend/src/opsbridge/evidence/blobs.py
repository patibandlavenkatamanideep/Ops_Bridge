"""Shared artifact bytes: storing them, and deleting them only when nothing uses them.

Artifact rows of one tenant share the bytes of identical content (one content-addressed
storage key). Bytes may be deleted only when no live row references them, and only after the
database has recorded that the rows are gone. A writer and a purge of the same content
serialize on a transaction-scoped advisory lock keyed by tenant and content digest: a writer
holds it from storing the bytes until its row commits, so a purge either sees that row (and
keeps the bytes) or finishes first (and the writer stores the bytes again, because the
stores skip a write only when the object already exists).
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from .storage import ArtifactStore


def lock_content(s: Session, tenant_id: uuid.UUID, sha_hex: str) -> None:
    s.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"artifact-bytes:{tenant_id}:{sha_hex}"},
    )


def store_bytes(
    s: Session, store: ArtifactStore, tenant_id: uuid.UUID, data: bytes
) -> tuple[str, str]:
    """`store.put` under the content lock, held until the caller's transaction ends, which
    must also insert the artifact row that references the returned key."""
    lock_content(s, tenant_id, hashlib.sha256(data).hexdigest())
    return store.put(tenant_id, data)
