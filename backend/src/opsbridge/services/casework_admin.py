"""Uploads, case conversations and tenant administration.

Uploads are untrusted: bounded size, content type checked against the bytes, safe
filenames, no URL fetching, and ingestion through the same parsers and pack mappings as
source-system records. Conversation messages are case-scoped; a correction supersedes a
message, a deletion removes its content but keeps who/when/why and the content hash
(a hash is not anonymization and is documented as such).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import TIERS, Principal, require
from opsbridge.canonical import digest
from opsbridge.evidence.ingest import IngestRejected, ingest
from opsbridge.evidence.storage import ArtifactStore
from opsbridge.mapping import PackSpec, RecordType
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    Case,
    ConversationMessage,
    Membership,
    Tenant,
    TenantSettings,
    User,
)

from .cases import _visible_filter
from .investigations import Conflict, NotFound

UPLOAD_KINDS: dict[str, RecordType | None] = {
    "supplier_message": "supplier_message",
    "invoice": "invoice",
    "goods_receipt": "goods_receipt",
    "attachment": None,
}

RETENTION_KEYS = (
    "raw_documents_days",
    "conversation_days",
    "model_artifacts_days",
    "tool_artifacts_days",
)


def _ctx(p: Principal) -> DbContext:
    return DbContext(tenant_id=p.tenant_id, user_id=p.user_id)


def _case(s: Session, p: Principal, case_id: uuid.UUID, lock: bool = False) -> Case:
    q = select(Case).where(Case.id == case_id, _visible_filter(p))
    case = s.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if case is None:
        raise NotFound("case")
    return case


# --- uploads ------------------------------------------------------------------------------


def upload_document(
    db: Database,
    store: ArtifactStore,
    pack: PackSpec,
    p: Principal,
    case_id: uuid.UUID,
    *,
    kind: str,
    filename: str | None,
    content_type: str,
    data: bytes,
    correlation_id: str | None,
) -> dict[str, Any]:
    require(p, "evidence.upload")
    if kind not in UPLOAD_KINDS:
        raise Conflict(
            "invalid_kind",
            f"Unsupported document kind {kind!r}.",
            {"allowed": sorted(UPLOAD_KINDS)},
        )
    record_type = UPLOAD_KINDS[kind]
    spec = pack.sources.get(record_type) if record_type else None
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id, lock=True)
        if case.status in ("resolved", "closed_without_resolution"):
            raise Conflict("case_closed", "Documents cannot be added to a closed case.")
        from opsbridge.evidence.parsers import safe_filename

        # Re-uploading a file with the same name creates a new version of the same document.
        name = safe_filename(filename) or digest({"bytes": data.hex()})[7:39]
        key = f"upload:{case.id}:{kind}:{name}"
        try:
            res = ingest(
                s,
                store,
                tenant_id=p.tenant_id,
                pack=pack if spec else None,
                record_type=record_type if spec else None,
                source_key=key,
                origin="upload",
                title=f"Uploaded {kind.replace('_', ' ')}",
                data=data,
                content_type=content_type,
                filename=filename,
                created_by=p.user_id,
            )
        except IngestRejected as exc:
            raise Conflict("upload_rejected", exc.reason) from exc
        s.execute(
            text(
                "INSERT INTO case_documents (tenant_id, case_id, document_id, role, added_by) "
                "VALUES (:t, :c, :d, :r, :u) ON CONFLICT DO NOTHING"
            ),
            {"t": p.tenant_id, "c": case.id, "d": res.document_id, "r": kind, "u": p.user_id},
        )
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="evidence.uploaded",
            resource_type="document",
            resource_id=str(res.document_id),
            correlation_id=correlation_id,
            details={
                "case_id": str(case.id),
                "kind": kind,
                "sha256": res.raw_sha256,
                "parse_status": res.parse_status,
            },
        )
        return {
            "document_id": str(res.document_id),
            "version_id": str(res.version_id),
            "version_no": res.version_no,
            "raw_sha256": res.raw_sha256,
            "parse_status": res.parse_status,
            "parse_detail": res.parse_detail,
            "problems": res.problems,
            "records": res.records,
        }


# --- conversation -------------------------------------------------------------------------


def list_messages(
    db: Database, p: Principal, case_id: uuid.UUID
) -> list[tuple[ConversationMessage, str | None]]:
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        return list(
            s.execute(
                select(ConversationMessage, User.display_name)
                .join(User, User.id == ConversationMessage.author_id, isouter=True)
                .where(ConversationMessage.case_id == case_id)
                .order_by(ConversationMessage.seq)
            ).all()
        )


def post_message(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    body: str,
    supersedes: uuid.UUID | None,
    correlation_id: str | None,
) -> ConversationMessage:
    require(p, "conversation.write")
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id, lock=True)
        if supersedes is not None:
            old = s.get(ConversationMessage, supersedes)
            if old is None or old.case_id != case.id:
                raise NotFound("message")
            if old.author_id != p.user_id:
                raise Conflict("not_author", "Only the author can correct a message.")
        seq = (
            s.execute(
                select(func.max(ConversationMessage.seq)).where(
                    ConversationMessage.case_id == case.id
                )
            ).scalar()
            or 0
        ) + 1
        msg = ConversationMessage(
            tenant_id=p.tenant_id,
            case_id=case.id,
            seq=seq,
            author_kind="user",
            author_id=p.user_id,
            body=body,
            body_sha256=digest({"body": body}),
            supersedes_id=supersedes,
        )
        s.add(msg)
        s.flush()
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="conversation.posted",
            resource_type="conversation_message",
            resource_id=str(msg.id),
            correlation_id=correlation_id,
            details={
                "case_id": str(case.id),
                "supersedes": str(supersedes) if supersedes else None,
            },
        )
        s.refresh(msg)
        return msg


def delete_message(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    message_id: uuid.UUID,
    reason: str,
    correlation_id: str | None,
) -> ConversationMessage:
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        msg = s.execute(
            select(ConversationMessage)
            .where(ConversationMessage.id == message_id, ConversationMessage.case_id == case_id)
            .with_for_update()
        ).scalar_one_or_none()
        if msg is None:
            raise NotFound("message")
        if msg.author_id != p.user_id and not p.has("tenant.manage_members"):
            raise Conflict(
                "not_author", "Only the author or a tenant administrator can delete a message."
            )
        if msg.deleted_at is None:
            msg.body = None  # content removed; hash and metadata kept (a hash is not anonymization)
            msg.deleted_at = datetime.now(UTC)
            msg.deleted_by = p.user_id
            msg.deletion_reason = reason[:500]
            audit.record(
                s,
                tenant_id=p.tenant_id,
                actor_kind="user",
                actor_id=str(p.user_id),
                action="conversation.deleted",
                resource_type="conversation_message",
                resource_id=str(msg.id),
                correlation_id=correlation_id,
                details={"case_id": str(case_id)},
            )
        s.flush()
        s.refresh(msg)
        return msg


# --- tenant settings and members ---------------------------------------------------------


def get_settings_view(db: Database, p: Principal) -> dict[str, Any]:
    require(p, "tenant.read_settings")
    with db.session(_ctx(p)) as s:
        return _settings_view(s, p.tenant_id)


def _settings_view(s: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
    ts = s.get(TenantSettings, tenant_id)
    tenant = s.get(Tenant, tenant_id)
    assert tenant is not None
    members = s.execute(
        select(Membership, User).join(User, User.id == Membership.user_id).order_by(User.username)
    ).all()
    return {
        "tenant": {
            "slug": tenant.slug,
            "name": tenant.name,
            "tier": tenant.tier,
            "pack_id": tenant.pack_id,
            "synthetic_demo": tenant.synthetic_demo,
        },
        "entitlements": {
            "model_modes": sorted(TIERS[tenant.tier].model_modes),
            "external_connectors": sorted(TIERS[tenant.tier].external_connectors),
            "local_inference": TIERS[tenant.tier].local_inference,
            "max_model_calls_per_run": TIERS[tenant.tier].max_model_calls_per_run,
        },
        "settings": {
            "model_mode": ts.model_mode if ts else "rules_only",
            "task_connector": ts.task_connector if ts else "sim_tasks",
            "retention": (ts.retention if ts else {}) or {},
        },
        "members": [
            {
                "username": u.username,
                "display_name": u.display_name,
                "roles": list(m.roles),
                "approval_scope": list(m.approval_scope or []),
                "status": m.status,
            }
            for m, u in members
        ],
    }


def update_settings(
    db: Database,
    p: Principal,
    *,
    model_mode: str | None,
    task_connector: str | None,
    retention: dict[str, int] | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    if task_connector is not None:
        require(p, "tenant.manage_connectors")
    if task_connector is None or model_mode is not None or retention is not None:
        require(p, "tenant.manage_policy")
    with db.session(_ctx(p)) as s:
        tenant = s.get(Tenant, p.tenant_id)
        assert tenant is not None
        tier = TIERS[tenant.tier]
        ts = s.get(TenantSettings, p.tenant_id)
        if ts is None:
            ts = TenantSettings(
                tenant_id=p.tenant_id,
                model_mode="rules_only",
                task_connector="sim_tasks",
                retention={},
            )
            s.add(ts)
        changes: dict[str, Any] = {}
        if model_mode is not None:
            if model_mode != "rules_only" and model_mode not in tier.model_modes:
                raise Conflict(
                    "not_entitled",
                    f"Model mode {model_mode!r} is not enabled for the {tier.tier} tier.",
                )
            ts.model_mode = model_mode
            changes["model_mode"] = model_mode
        if task_connector is not None:
            if task_connector not in tier.external_connectors:
                raise Conflict(
                    "not_entitled", f"Connector {task_connector!r} is not enabled for this tier."
                )
            ts.task_connector = task_connector
            changes["task_connector"] = task_connector
        if retention is not None:
            bad = [k for k in retention if k not in RETENTION_KEYS]
            if bad or any(not isinstance(v, int) or v < 0 or v > 3650 for v in retention.values()):
                raise Conflict(
                    "invalid_retention",
                    "Retention keys or values are invalid.",
                    {"allowed_keys": list(RETENTION_KEYS)},
                )
            ts.retention = dict(retention)
            changes["retention"] = retention
        ts.updated_by = p.user_id
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="tenant.settings_updated",
            resource_type="tenant",
            resource_id=str(p.tenant_id),
            correlation_id=correlation_id,
            details={"changes": changes},
        )
        s.flush()
        return _settings_view(s, p.tenant_id)
