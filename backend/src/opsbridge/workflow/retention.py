"""Retention sweep: delete protected content past the tenant's retention periods.

Deletes bytes and free text, not the fact that something existed: artifact manifests keep
their digests and a deletion reason, audit events keep opaque references. After deletion,
semantic review or replay of the affected material is no longer possible, and the UI and
API say so instead of failing silently. A digest is not anonymization of low-entropy data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from opsbridge.audit import events as audit
from opsbridge.evidence.storage import ArtifactStore
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    Artifact,
    ConversationMessage,
    DocumentVersion,
    EvidenceChunk,
    Job,
    TenantSettings,
)

KINDS = {
    "raw_documents_days": ("raw_document",),
    "model_artifacts_days": ("model_request", "model_response"),
    "tool_artifacts_days": ("tool_request", "tool_response"),
}


@dataclass
class SweepReport:
    artifacts_deleted: int = 0
    chunks_deleted: int = 0
    messages_cleared: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "artifacts_deleted": self.artifacts_deleted,
            "chunks_deleted": self.chunks_deleted,
            "messages_cleared": self.messages_cleared,
        }


def sweep(
    db: Database, store: ArtifactStore, tenant_id: uuid.UUID, now: datetime | None = None
) -> SweepReport:
    report = SweepReport()
    with db.session(DbContext(tenant_id=tenant_id)) as s:
        # Cut-offs are compared with timestamps the database wrote, so use its clock: a
        # drifting application clock would otherwise delete content early or late.
        now = now or s.execute(select(func.now())).scalar_one()
        ts = s.get(TenantSettings, tenant_id)
        policy: dict[str, Any] = dict(ts.retention) if ts and ts.retention else {}
        for key, kinds in KINDS.items():
            if key not in policy:
                continue
            cutoff = now - timedelta(days=int(policy[key]))
            rows = list(
                s.execute(
                    select(Artifact).where(
                        Artifact.kind.in_(kinds),
                        Artifact.deleted_at.is_(None),
                        Artifact.created_at <= cutoff,
                    )
                ).scalars()
            )
            for art in rows:
                if art.storage_key:
                    store.delete(tenant_id, art.storage_key)
                art.storage_key = None
                art.deleted_at = now
                art.deletion_reason = f"retention:{key}={policy[key]}"
                report.artifacts_deleted += 1
                if art.kind == "raw_document":
                    versions = select(DocumentVersion.id).where(
                        DocumentVersion.raw_artifact_id == art.id
                    )
                    report.chunks_deleted += _rowcount(
                        s.execute(
                            delete(EvidenceChunk).where(EvidenceChunk.version_id.in_(versions))
                        )
                    )
        if "conversation_days" in policy:
            cutoff = now - timedelta(days=int(policy["conversation_days"]))
            for msg in s.execute(
                select(ConversationMessage).where(
                    ConversationMessage.deleted_at.is_(None),
                    ConversationMessage.created_at <= cutoff,
                )
            ).scalars():
                msg.body = None
                msg.deleted_at = now
                msg.deletion_reason = f"retention:conversation_days={policy['conversation_days']}"
                report.messages_cleared += 1
        audit.record(
            s,
            tenant_id=tenant_id,
            actor_kind="service",
            actor_id="service:opsbridge-worker",
            action="retention.sweep",
            resource_type="tenant",
            resource_id=str(tenant_id),
            details=report.to_dict(),
        )
    return report


def _rowcount(result: Any) -> int:
    return int(cast(CursorResult[Any], result).rowcount or 0)


def schedule_due(db: Database) -> int:
    """Enqueue one retention sweep per tenant with retention configured, per database day.

    Tenant ids come from `ops.tenants_with_retention()` (aggregate-only, migration 0005); the
    dedupe key `retention:<tenant>:<date>` makes concurrent schedulers enqueue at most one
    sweep per tenant per day. Returns how many jobs this call enqueued.
    """
    enqueued = 0
    with db.session(DbContext()) as s:
        day = s.execute(select(func.current_date())).scalar_one()
        tenants = list(s.execute(text("SELECT * FROM ops.tenants_with_retention()")).scalars())
        for tenant_id in tenants:
            result = s.execute(
                pg_insert(Job)
                .values(
                    tenant_id=tenant_id,
                    kind="retention_sweep",
                    payload={"requested_by": "schedule"},
                    status="queued",
                    priority=200,
                    attempts=0,
                    max_attempts=3,
                    fencing_token=0,
                    dedupe_key=f"retention:{tenant_id}:{day.isoformat()}",
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(Job.id)
            )
            enqueued += 1 if result.first() is not None else 0  # nothing returned on conflict
    return enqueued
