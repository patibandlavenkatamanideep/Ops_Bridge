"""Case intake and queue service. Enforces permissions for every entry point."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import Forbidden, Principal, require
from opsbridge.domain import identifiers
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Case, User

CASE_STATUSES = (
    "new",
    "collecting_evidence",
    "investigating",
    "needs_information",
    "awaiting_review",
    "follow_up_pending",
    "resolved",
    "closed_without_resolution",
)

# Legal case transitions. Who may trigger them is checked by the calling service.
CASE_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"collecting_evidence", "closed_without_resolution"}),
    "collecting_evidence": frozenset(
        {"investigating", "needs_information", "closed_without_resolution"}
    ),
    "investigating": frozenset(
        {
            "needs_information",
            "awaiting_review",
            "collecting_evidence",
            "resolved",
            "closed_without_resolution",
        }
    ),
    "needs_information": frozenset(
        {"collecting_evidence", "awaiting_review", "closed_without_resolution"}
    ),
    "awaiting_review": frozenset(
        {
            "follow_up_pending",
            "collecting_evidence",
            "needs_information",
            "resolved",
            "closed_without_resolution",
        }
    ),
    "follow_up_pending": frozenset(
        {"collecting_evidence", "awaiting_review", "resolved", "closed_without_resolution"}
    ),
    "resolved": frozenset(),
    "closed_without_resolution": frozenset(),
}


class InvalidTransition(Exception):
    pass


def transition(case: Case, new_status: str) -> None:
    if new_status == case.status:
        return
    if new_status not in CASE_TRANSITIONS[case.status]:
        raise InvalidTransition(f"{case.status} -> {new_status}")
    case.status = new_status
    case.row_version += 1


@dataclass(frozen=True)
class CaseCreate:
    title: str
    description: str
    invoice_number: str | None
    po_number: str | None
    supplier_id: str | None = None
    # The caller checked that a similar-looking invoice number names a different document.
    confirm_distinct: bool = False


class DuplicateCase(Exception):
    def __init__(self, existing_id: uuid.UUID | None) -> None:
        super().__init__("a case for this source already exists")
        self.existing_id = existing_id


class PossibleDuplicate(Exception):
    """An existing case's invoice number differs only in punctuation, spacing or leading
    zeros. It may be the same invoice; a person must confirm before a second case opens."""

    def __init__(self, existing_id: uuid.UUID | None, existing_invoice_number: str | None) -> None:
        super().__init__("a case for a similar invoice number exists")
        self.existing_id = existing_id
        self.existing_invoice_number = existing_invoice_number


class IdempotencyKeyReused(Exception):
    """The idempotency key was already used for a different request."""


def _visible_filter(principal: Principal) -> Any:
    if principal.has("case.read_all") or principal.has("case.read_metadata"):
        return text("true")
    if principal.has("case.read_own"):
        return Case.requester_id == principal.user_id
    raise Forbidden("case.read_own")


def _next_number(session: Session, tenant_id: uuid.UUID) -> str:
    value = session.execute(
        text(
            "INSERT INTO tenant_counters (tenant_id, name, value) VALUES (:t, 'case', 1) "
            "ON CONFLICT (tenant_id, name) DO UPDATE SET value = tenant_counters.value + 1 "
            "RETURNING value"
        ),
        {"t": tenant_id},
    ).scalar_one()
    return f"CASE-{int(value):05d}"


def create_case(
    db: Database,
    principal: Principal,
    data: CaseCreate,
    idempotency_key: str | None,
    correlation_id: str | None = None,
) -> tuple[Case, bool]:
    """Create a case. Returns (case, created). Repeating an idempotency key with the same
    request returns the original; with a different request it is refused."""
    require(principal, "case.create")
    invoice = identifiers.normalize(data.invoice_number)
    po = identifiers.normalize(data.po_number)
    supplier = identifiers.normalize(data.supplier_id)
    if invoice:
        # Without a supplier the key is the one used before suppliers were recorded.
        source_key = f"invoice@{supplier}:{invoice}" if supplier else f"invoice:{invoice}"
    elif idempotency_key:
        source_key = f"manual:{principal.user_id}:{idempotency_key}"
    else:
        source_key = f"manual:{uuid.uuid4()}"
    ctx = DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)
    with db.session(ctx) as s:
        existing = s.execute(select(Case).where(Case.source_key == source_key)).scalar_one_or_none()
        if existing is not None:
            same_requester = existing.requester_id == principal.user_id
            if idempotency_key and same_requester:
                asked = (data.title.strip(), data.description.strip(), invoice, po, supplier)
                if asked != (
                    existing.title,
                    existing.description,
                    existing.invoice_number,
                    existing.po_number,
                    existing.supplier_id,
                ):
                    raise IdempotencyKeyReused()
                return existing, False
            visible = principal.has("case.read_all") or same_requester
            raise DuplicateCase(existing.id if visible else None)
        if invoice and not data.confirm_distinct:
            looks_alike = text(
                f"{identifiers.similarity_sql('cases.invoice_number')} = :key"
            ).bindparams(key=identifiers.similarity_key(invoice))
            # Different named suppliers make different invoices; otherwise it may be the same.
            supplier_may_match = (
                [or_(Case.supplier_id.is_(None), Case.supplier_id == supplier)] if supplier else []
            )
            similar = s.execute(
                select(Case)
                .where(Case.invoice_number != invoice, looks_alike, *supplier_may_match)
                .limit(1)
            ).scalar_one_or_none()
            if similar is not None:
                visible = (
                    principal.has("case.read_all") or similar.requester_id == principal.user_id
                )
                raise PossibleDuplicate(
                    similar.id if visible else None, similar.invoice_number if visible else None
                )
        case = Case(
            tenant_id=principal.tenant_id,
            case_number=_next_number(s, principal.tenant_id),
            title=data.title.strip(),
            description=data.description.strip(),
            invoice_number=invoice,
            po_number=po,
            supplier_id=supplier,
            source_key=source_key,
            discrepancy_types=[],
            evidence_readiness="not_collected",
            status="new",
            requester_id=principal.user_id,
            row_version=1,
        )
        s.add(case)
        try:
            s.flush()
        except IntegrityError as exc:  # concurrent duplicate intake
            raise DuplicateCase(None) from exc
        audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor_kind="user",
            actor_id=str(principal.user_id),
            action="case.created",
            resource_type="case",
            resource_id=str(case.id),
            correlation_id=correlation_id,
            details={"case_number": case.case_number},
        )
        return case, True


def get_case(db: Database, principal: Principal, case_id: uuid.UUID) -> Case | None:
    ctx = DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)
    with db.session(ctx) as s:
        return s.execute(
            select(Case).where(Case.id == case_id, _visible_filter(principal))
        ).scalar_one_or_none()


@dataclass(frozen=True)
class QueueItem:
    case: Case
    assignee_name: str | None


def encode_cursor(created_at: datetime, case_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(case_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    created, cid = json.loads(base64.urlsafe_b64decode(padded.encode()))
    return datetime.fromisoformat(created), uuid.UUID(cid)


def list_cases(
    db: Database,
    principal: Principal,
    *,
    status: str | None,
    search: str | None = None,
    cursor: str | None,
    limit: int,
) -> tuple[list[QueueItem], str | None]:
    ctx = DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)
    with db.session(ctx) as s:
        q = (
            select(Case, User.display_name)
            .outerjoin(User, User.id == Case.assignee_id)
            .where(_visible_filter(principal))
        )
        if status:
            q = q.where(Case.status == status)
        if search:
            pattern = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            q = q.where(
                or_(
                    Case.case_number.ilike(pattern, escape="\\"),
                    Case.invoice_number.ilike(pattern, escape="\\"),
                    Case.po_number.ilike(pattern, escape="\\"),
                    Case.title.ilike(pattern, escape="\\"),
                )
            )
        if cursor:
            created_at, cid = decode_cursor(cursor)
            q = q.where(
                or_(
                    Case.created_at < created_at, and_(Case.created_at == created_at, Case.id < cid)
                )
            )
        rows = s.execute(q.order_by(Case.created_at.desc(), Case.id.desc()).limit(limit + 1)).all()
        items = [QueueItem(c, name) for c, name in rows[:limit]]
        next_cursor = (
            encode_cursor(items[-1].case.created_at, items[-1].case.id)
            if len(rows) > limit
            else None
        )
        return items, next_cursor
