"""Investigation runs, evidence access, corrections and evidence-based resolution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import false, select, text
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import Forbidden, Principal, require
from opsbridge.domain.records import SourceRef
from opsbridge.evidence.citations import resolve_all
from opsbridge.evidence.storage import ArtifactStore
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    Artifact,
    Case,
    CaseDocument,
    DocumentVersion,
    EvidenceChunk,
    ExtractedField,
    FieldCorrection,
    InvestigationVersion,
    SourceDocument,
    TenantSettings,
    User,
    WorkflowRun,
    WorkflowStep,
)
from opsbridge.workflow import jobs

from .cases import _visible_filter, transition


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _ctx(p: Principal) -> DbContext:
    return DbContext(tenant_id=p.tenant_id, user_id=p.user_id)


def _case(session: Session, p: Principal, case_id: uuid.UUID, lock: bool = False) -> Case:
    q = select(Case).where(Case.id == case_id, _visible_filter(p))
    if lock:
        q = q.with_for_update()
    case = session.execute(q).scalar_one_or_none()
    if case is None:
        raise NotFound("case")
    return case


class QuotaExceeded(Exception):
    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _check_run_quota(s: Session, p: Principal) -> None:
    """Organization-wide run quota per rolling hour (tier entitlement; not an authority).

    The window is computed with the database clock, the same clock that wrote `created_at`;
    mixing it with the application host's clock breaks the window when they drift.
    """
    limit = p.tier.max_investigations_per_hour
    row = s.execute(
        text(
            "SELECT count(*), extract(epoch FROM ("
            "  (SELECT created_at FROM workflow_runs WHERE kind = 'investigation' "
            "     AND created_at > now() - interval '1 hour' "
            "   ORDER BY created_at DESC OFFSET :k LIMIT 1) + interval '1 hour' - now())) "
            "FROM workflow_runs WHERE kind = 'investigation' AND created_at > now() - interval '1 hour'"
        ),
        {"k": max(limit - 1, 0)},
    ).one()
    count, seconds_left = int(row[0]), row[1]
    if count >= limit:
        retry = max(1, int(seconds_left or 0) + 1)
        raise QuotaExceeded(
            f"Your organization has started {count} investigations in the last hour "
            f"(limit {limit} for the {p.tier.tier} tier).",
            retry,
        )


def start_investigation(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    model_mode: str | None,
    correlation_id: str | None,
    *,
    enforce_quota: bool = True,
) -> tuple[WorkflowRun, bool]:
    require(p, "investigation.run")
    with db.session(_ctx(p)) as s:
        settings_row = s.get(TenantSettings, p.tenant_id)
        mode = model_mode or (settings_row.model_mode if settings_row else "rules_only")
        if mode != "rules_only" and mode not in p.tier.model_modes:
            # Tier entitlement is a feature gate, not an authorization grant.
            raise Conflict(
                "not_entitled",
                f"Model mode {mode!r} is not enabled for the {p.tier.tier} tier.",
                {"allowed": sorted(p.tier.model_modes)},
            )
        case = _case(s, p, case_id, lock=True)
        if case.status in ("resolved", "closed_without_resolution"):
            raise Conflict("case_closed", "A closed case cannot be investigated again.")
        if enforce_quota:
            _check_run_quota(s, p)
        active = (
            s.execute(
                select(WorkflowRun).where(
                    WorkflowRun.case_id == case.id, WorkflowRun.status.in_(("queued", "running"))
                )
            )
            .scalars()
            .first()
        )
        if active is not None:
            if active.model_mode != mode:
                # Do not silently substitute a different run for the one the user asked for.
                raise Conflict(
                    "run_in_progress",
                    "Another investigation run is in progress for this case.",
                    {"run_id": str(active.id), "model_mode": active.model_mode},
                )
            return active, False
        run = WorkflowRun(
            tenant_id=p.tenant_id,
            case_id=case.id,
            kind="investigation",
            status="queued",
            workflow_version="investigation/1.1.0",
            model_mode=mode,
            response_mode="live",
            requested_by=p.user_id,
            budget={},
            usage={},
        )
        s.add(run)
        s.flush()
        jobs.enqueue(
            s,
            tenant_id=p.tenant_id,
            kind="investigate",
            payload={"run_id": str(run.id)},
            dedupe_key=f"investigate:{run.id}",
        )
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="investigation.requested",
            resource_type="case",
            resource_id=str(case.id),
            correlation_id=correlation_id,
            details={"run_id": str(run.id)},
        )
        return run, True


def get_run(
    db: Database, p: Principal, case_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[WorkflowRun, list[WorkflowStep]]:
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        run = s.execute(
            select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.case_id == case_id)
        ).scalar_one_or_none()
        if run is None:
            raise NotFound("run")
        steps = list(
            s.execute(
                select(WorkflowStep).where(WorkflowStep.run_id == run.id).order_by(WorkflowStep.seq)
            ).scalars()
        )
        return run, steps


@dataclass
class InvestigationView:
    version: InvestigationVersion
    citations: list[dict[str, Any]]
    evidence_visible: bool
    versions: list[dict[str, Any]]
    model_routing: dict[str, Any] | None = None  # how the producing run routed model use


def latest_investigation(
    db: Database, p: Principal, case_id: uuid.UUID, version: int | None = None
) -> InvestigationView | None:
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        q = select(InvestigationVersion).where(InvestigationVersion.case_id == case_id)
        q = (
            q.where(InvestigationVersion.version == version)
            if version
            else q.order_by(InvestigationVersion.version.desc())
        )
        iv = s.execute(q.limit(1)).scalar_one_or_none()
        if iv is None:
            return None
        visible = p.has("evidence.read")
        refs = [SourceRef(**e) for f in iv.findings for e in f.get("evidence", [])]
        citations = (
            [c.to_dict(include_excerpt=visible) for c in resolve_all(s, refs)] if visible else []
        )
        history = [
            {
                "version": v.version,
                "outcome": v.outcome,
                "created_at": v.created_at.isoformat(),
                "id": str(v.id),
            }
            for v in s.execute(
                select(InvestigationVersion)
                .where(InvestigationVersion.case_id == case_id)
                .order_by(InvestigationVersion.version.desc())
            ).scalars()
        ]
        run = s.get(WorkflowRun, iv.run_id) if iv.run_id else None
        routing = dict((run.usage or {}).get("routing") or {}) if run is not None else None
        return InvestigationView(iv, citations, visible, history, routing or None)


def _linked_version(
    s: Session, case_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[DocumentVersion, SourceDocument]:
    row = s.execute(
        select(DocumentVersion, SourceDocument)
        .join(SourceDocument, SourceDocument.id == DocumentVersion.document_id)
        .join(
            CaseDocument,
            (CaseDocument.document_id == SourceDocument.id) & (CaseDocument.case_id == case_id),
        )
        .where(DocumentVersion.id == version_id)
    ).first()
    if row is None:
        raise NotFound("document")
    return row[0], row[1]


def case_documents(db: Database, p: Principal, case_id: uuid.UUID) -> list[dict[str, Any]]:
    require(p, "evidence.read")
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        docs = s.execute(
            select(SourceDocument, CaseDocument.role)
            .join(CaseDocument, CaseDocument.document_id == SourceDocument.id)
            .where(CaseDocument.case_id == case_id)
            .order_by(SourceDocument.kind, SourceDocument.title)
        ).all()
        out = []
        for doc, role in docs:
            versions = s.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.desc())
            ).scalars()
            out.append(
                {
                    "document_id": str(doc.id),
                    "title": doc.title,
                    "kind": doc.kind,
                    "role": role,
                    "origin": doc.origin,
                    "classification": doc.classification,
                    "versions": [
                        {
                            "version_id": str(v.id),
                            "version_no": v.version_no,
                            "raw_sha256": v.raw_sha256,
                            "normalized_sha256": v.normalized_sha256,
                            "normalization_version": v.normalization_version,
                            "content_type": v.content_type,
                            "page_count": v.page_count,
                            "parse_status": v.parse_status,
                            "parse_detail": v.parse_detail,
                            "created_at": v.created_at.isoformat(),
                        }
                        for v in versions
                    ],
                }
            )
        return out


def version_detail(
    db: Database, p: Principal, case_id: uuid.UUID, version_id: uuid.UUID
) -> dict[str, Any]:
    require(p, "evidence.read")
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        version, doc = _linked_version(s, case_id, version_id)
        fields = list(
            s.execute(
                select(ExtractedField)
                .where(ExtractedField.version_id == version.id)
                .order_by(ExtractedField.created_at, ExtractedField.field_path)
            ).scalars()
        )
        corrections = s.execute(
            select(FieldCorrection, User.display_name)
            .join(User, User.id == FieldCorrection.author_id, isouter=True)
            .where(FieldCorrection.field_id.in_([f.id for f in fields]) if fields else false())
            .order_by(FieldCorrection.created_at)
        ).all()
        by_field: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for c, name in corrections:
            by_field.setdefault(c.field_id, []).append(
                {
                    "correction_id": str(c.id),
                    "previous_value": c.previous_value,
                    "new_value": c.new_value,
                    "reason": c.reason,
                    "author": name or str(c.author_id),
                    "created_at": c.created_at.isoformat(),
                }
            )
        chunks = s.execute(
            select(EvidenceChunk)
            .where(EvidenceChunk.version_id == version.id)
            .order_by(EvidenceChunk.seq)
            .limit(200)
        ).scalars()
        return {
            "document": {"document_id": str(doc.id), "title": doc.title, "kind": doc.kind},
            "version": {
                "version_id": str(version.id),
                "version_no": version.version_no,
                "raw_sha256": version.raw_sha256,
                "normalized_sha256": version.normalized_sha256,
                "normalization_version": version.normalization_version,
                "parser": version.parser,
                "parse_status": version.parse_status,
                "parse_detail": version.parse_detail,
                "content_type": version.content_type,
            },
            "fields": [
                {
                    "field_id": str(f.id),
                    "record_type": f.record_type,
                    "record_key": f.record_key,
                    "field_path": f.field_path,
                    "original_value": f.value_text,
                    "current_value": (by_field.get(f.id) or [{}])[-1].get(
                        "new_value", f.value_text
                    ),
                    "value_type": f.value_type,
                    "locator": f.locator,
                    "method": f.method,
                    "status": f.status,
                    "note": f.note,
                    "corrections": by_field.get(f.id, []),
                }
                for f in fields
            ],
            "chunks": [
                {
                    "locator": c.locator,
                    "page": c.page,
                    "line_start": c.line_start,
                    "line_end": c.line_end,
                    "text": c.text_content,
                }
                for c in chunks
            ],
        }


def correct_field(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    field_id: uuid.UUID,
    new_value: str,
    reason: str,
    expected_current: str | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    require(p, "evidence.correct")
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id, lock=True)
        if case.status in ("resolved", "closed_without_resolution"):
            raise Conflict("case_closed", "Evidence of a closed case cannot be corrected.")
        fld = s.execute(
            select(ExtractedField).where(ExtractedField.id == field_id).with_for_update()
        ).scalar_one_or_none()
        if fld is None:
            raise NotFound("field")
        _linked_version(s, case_id, fld.version_id)  # field must belong to this case's evidence
        if fld.method == "ocr":
            # A correction would mark an OCR guess "corrected" without making it usable; the
            # confirmed value belongs in the source system, from where a re-run picks it up.
            raise Conflict(
                "ocr_candidate",
                "Values read by OCR are review candidates and are not used by the matching "
                "rules. Record the confirmed value in the source system and re-run.",
            )
        last = s.execute(
            select(FieldCorrection)
            .where(FieldCorrection.field_id == fld.id)
            .order_by(FieldCorrection.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        current = last.new_value if last else fld.value_text
        if expected_current is not None and expected_current != current:
            raise Conflict(
                "stale_version",
                "The field changed since you loaded it.",
                {"current_value": current},
            )
        if fld.value_type == "decimal":
            from opsbridge.domain.money import DecimalParseError, parse_decimal

            try:
                parse_decimal(new_value)
            except DecimalParseError as exc:
                raise Conflict("invalid_value", f"Not a valid decimal: {exc}") from exc
        corr = FieldCorrection(
            tenant_id=p.tenant_id,
            field_id=fld.id,
            previous_value=current,
            new_value=new_value,
            reason=reason,
            author_id=p.user_id,
        )
        s.add(corr)
        if fld.status != "corrected":
            fld.status = "corrected"
        s.flush()
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="evidence.corrected",
            resource_type="extracted_field",
            resource_id=str(fld.id),
            correlation_id=correlation_id,
            details={"case_id": str(case_id), "field": fld.field_path},
        )
        return {
            "correction_id": str(corr.id),
            "field_id": str(fld.id),
            "previous_value": current,
            "new_value": new_value,
        }


def raw_document(
    db: Database, store: ArtifactStore, p: Principal, case_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[bytes, str, str]:
    require(p, "evidence.read")
    with db.session(_ctx(p)) as s:
        _case(s, p, case_id)
        version, doc = _linked_version(s, case_id, version_id)
        art = s.get(Artifact, version.raw_artifact_id) if version.raw_artifact_id else None
        if art is None or art.storage_key is None or art.deleted_at is not None:
            raise NotFound("original (withheld or deleted by retention policy)")
        data = store.get(p.tenant_id, art.storage_key)
        name = version.safe_filename or f"{doc.kind}-v{version.version_no}"
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="evidence.downloaded",
            resource_type="document_version",
            resource_id=str(version.id),
        )
        return data, version.content_type, name


def resolve_case(
    db: Database, p: Principal, case_id: uuid.UUID, note: str, correlation_id: str | None
) -> Case:
    """Mark the business exception resolved — only when the latest investigation found no discrepancy."""
    require(p, "investigation.run")
    with db.session(_ctx(p)) as s:
        case = _case(s, p, case_id, lock=True)
        latest = s.execute(
            select(InvestigationVersion)
            .where(InvestigationVersion.case_id == case_id)
            .order_by(InvestigationVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None or latest.outcome != "matched":
            raise Conflict(
                "resolution_evidence_missing",
                "Resolution requires a current investigation showing the invoice now matches policy. "
                "Creating a follow-up task does not resolve a discrepancy.",
            )
        if case.status not in ("awaiting_review", "follow_up_pending", "investigating"):
            raise Conflict("invalid_transition", f"Cannot resolve a case in status {case.status}.")
        from .freshness import stale_reason
        from .proposals import evidence_state

        changed = stale_reason(s, case.id, latest)
        if changed is None and evidence_state(s, case)[1] != latest.policy_version_id:
            changed = "the effective policy changed since the investigation; re-run it"
        if changed is not None:
            raise Conflict("stale_investigation", changed[0].upper() + changed[1:] + ".")
        transition(case, "resolved")
        s.flush()
        s.refresh(case)  # load server-generated updated_at before the session closes
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="case.resolved",
            resource_type="case",
            resource_id=str(case.id),
            correlation_id=correlation_id,
            details={"investigation_version": latest.version, "note_len": len(note)},
        )
        return case


__all__ = ["Conflict", "Forbidden", "NotFound"]
