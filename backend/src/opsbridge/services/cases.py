"""Case intake and queue service. Enforces permissions for every entry point."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import Forbidden, Principal, require
from opsbridge.canonical import digest
from opsbridge.domain import identifiers
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Case, Membership, Operation, Proposal, User

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
    request returns the case it created; with a different request it is refused. Another
    key for an invoice that already has a case is a duplicate, not a replay."""
    require(principal, "case.create")
    invoice = identifiers.normalize(data.invoice_number)
    po = identifiers.normalize(data.po_number)
    supplier = identifiers.normalize(data.supplier_id)
    request_digest = digest(
        {
            "title": data.title.strip(),
            "description": data.description.strip(),
            "invoice_number": invoice,
            "po_number": po,
            "supplier_id": supplier,
        }
    )
    if invoice:
        # Without a supplier the key is the one used before suppliers were recorded.
        source_key = f"invoice@{supplier}:{invoice}" if supplier else f"invoice:{invoice}"
    elif idempotency_key:
        source_key = f"manual:{principal.user_id}:{idempotency_key}"
    else:
        source_key = f"manual:{uuid.uuid4()}"
    ctx = DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)
    with db.session(ctx) as s:
        if idempotency_key:
            replayed = s.execute(
                select(Case).where(
                    Case.requester_id == principal.user_id, Case.intake_key == idempotency_key
                )
            ).scalar_one_or_none()
            if replayed is not None:
                if replayed.intake_digest != request_digest:
                    raise IdempotencyKeyReused()
                return replayed, False
        existing = s.execute(select(Case).where(Case.source_key == source_key)).scalar_one_or_none()
        if existing is not None:
            same_requester = existing.requester_id == principal.user_id
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
            intake_key=idempotency_key,
            intake_digest=request_digest,
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
    owner_active: bool = True  # the owner is still an active investigator here
    awaiting_approval: bool = False  # a proposal waits for a decision
    needs_review: bool = False  # an operation's outcome is unknown: a person must check it


CLOSED_STATUSES = ("resolved", "closed_without_resolution")
OPEN_STATUSES = tuple(s for s in CASE_STATUSES if s not in CLOSED_STATUSES)
# Built-in queue views. Each is a URL parameter, so a view can be bookmarked or shared.
QUEUE_VIEWS = ("all", "mine", "unassigned", "awaiting_approval", "needs_review")
QUEUE_SORTS = ("newest", "oldest")


class OwnerChanged(Exception):
    """Someone else changed the case's owner since the caller looked."""

    def __init__(self, current: uuid.UUID | None) -> None:
        super().__init__("owner changed")
        self.current = current


class NotAssignable(Exception):
    """The case cannot be given to that person, or not in its present state."""


def _active_investigator(user_id: Any) -> Any:
    """`user_id` is an active investigator of the case's own tenant (a member's rows in other
    tenants are visible to that member, so the tenant is pinned explicitly)."""
    return (
        select(Membership.id)
        .where(
            Membership.user_id == user_id,
            Membership.tenant_id == Case.tenant_id,
            Membership.status == "active",
            Membership.roles.contains(["investigator"]),
        )
        .exists()
    )


def _awaiting_approval() -> Any:
    return (
        select(Proposal.id)
        .where(Proposal.case_id == Case.id, Proposal.status == "awaiting_approval")
        .exists()
    )


def _needs_review() -> Any:
    return (
        select(Operation.id)
        .where(
            Operation.case_id == Case.id,
            Operation.status == "outcome_unknown",
            Operation.review_required.is_(True),
        )
        .exists()
    )


def _view_filter(view: str, principal: Principal) -> Any:
    if view == "mine":
        return and_(Case.assignee_id == principal.user_id, Case.status.in_(OPEN_STATUSES))
    if view == "unassigned":  # nobody, or someone who no longer works here as an investigator
        return and_(
            Case.status.in_(OPEN_STATUSES),
            or_(Case.assignee_id.is_(None), ~_active_investigator(Case.assignee_id)),
        )
    if view == "awaiting_approval":
        return _awaiting_approval()
    if view == "needs_review":
        return _needs_review()
    return text("true")


def view_counts(db: Database, principal: Principal) -> dict[str, int]:
    """How many cases each built-in view holds for this person (all views but `all`)."""
    ctx = DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)
    with db.session(ctx) as s:
        return {
            view: int(
                s.execute(
                    select(func.count())
                    .select_from(Case)
                    .where(_visible_filter(principal), _view_filter(view, principal))
                ).scalar_one()
            )
            for view in QUEUE_VIEWS
            if view != "all"
        }


def set_owner(
    db: Database,
    p: Principal,
    case_id: uuid.UUID,
    *,
    owner_id: uuid.UUID | None,
    expected_owner_id: uuid.UUID | None,
    correlation_id: str | None,
) -> Case:
    """Take, assign or release a case. `expected_owner_id` is the owner the caller saw: if
    someone else changed it since, nothing changes (OwnerChanged), so one person's claim is
    never silently overwritten by another's."""
    require(p, "case.assign")
    ctx = DbContext(tenant_id=p.tenant_id, user_id=p.user_id)
    with db.session(ctx) as s:
        case = s.execute(
            select(Case).where(Case.id == case_id, _visible_filter(p)).with_for_update()
        ).scalar_one_or_none()
        if case is None:
            raise LookupError("case")
        if case.status in CLOSED_STATUSES:
            raise NotAssignable("A closed case keeps the owner it had.")
        if case.assignee_id != expected_owner_id:
            raise OwnerChanged(case.assignee_id)
        if owner_id is not None:
            ok = s.execute(
                select(
                    select(Membership.id)
                    .where(
                        Membership.user_id == owner_id,
                        Membership.tenant_id == p.tenant_id,
                        Membership.status == "active",
                        Membership.roles.contains(["investigator"]),
                    )
                    .exists()
                )
            ).scalar_one()
            if not ok:
                raise NotAssignable(
                    "Only an active investigator of this organization can own a case."
                )
        if case.assignee_id != owner_id:
            previous = case.assignee_id
            case.assignee_id = owner_id
            case.row_version += 1
            audit.record(
                s,
                tenant_id=p.tenant_id,
                actor_kind="user",
                actor_id=str(p.user_id),
                action="case.owner_changed",
                resource_type="case",
                resource_id=str(case.id),
                correlation_id=correlation_id,
                details={
                    "from": str(previous) if previous else None,
                    "to": str(owner_id) if owner_id else None,
                },
            )
            s.flush()
        s.refresh(case)
        return case


def owner_name(db: Database, p: Principal, owner_id: uuid.UUID | None) -> str | None:
    if owner_id is None:
        return None
    with db.session(DbContext(tenant_id=p.tenant_id, user_id=p.user_id)) as s:
        user = s.get(User, owner_id)
        return (user.display_name or user.username) if user else None


def assignable_members(db: Database, p: Principal) -> list[dict[str, str]]:
    """Who a case can be given to: active investigators of this organization."""
    require(p, "case.assign")
    ctx = DbContext(tenant_id=p.tenant_id, user_id=p.user_id)
    with db.session(ctx) as s:
        rows = s.execute(
            select(User.id, User.username, User.display_name)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.tenant_id == p.tenant_id,
                Membership.status == "active",
                Membership.roles.contains(["investigator"]),
            )
            .order_by(User.display_name, User.username)
        ).all()
    return [{"id": str(i), "username": u, "display_name": d or u} for i, u, d in rows]


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
    view: str = "all",
    sort: str = "newest",
) -> tuple[list[QueueItem], str | None]:
    ctx = DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)
    with db.session(ctx) as s:
        q = (
            select(
                Case,
                User.display_name,
                _active_investigator(Case.assignee_id).label("owner_active"),
                _awaiting_approval().label("awaiting_approval"),
                _needs_review().label("needs_review"),
            )
            .outerjoin(User, User.id == Case.assignee_id)
            .where(_visible_filter(principal), _view_filter(view, principal))
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
        oldest = sort == "oldest"
        if cursor:
            created_at, cid = decode_cursor(cursor)
            q = q.where(
                or_(
                    Case.created_at > created_at, and_(Case.created_at == created_at, Case.id > cid)
                )
                if oldest
                else or_(
                    Case.created_at < created_at, and_(Case.created_at == created_at, Case.id < cid)
                )
            )
        order = (
            (Case.created_at.asc(), Case.id.asc())
            if oldest
            else (Case.created_at.desc(), Case.id.desc())
        )
        rows = s.execute(q.order_by(*order).limit(limit + 1)).all()
        items = [
            QueueItem(c, name, bool(active) or c.assignee_id is None, bool(waiting), bool(review))
            for c, name, active, waiting, review in rows[:limit]
        ]
        next_cursor = (
            encode_cursor(items[-1].case.created_at, items[-1].case.id)
            if len(rows) > limit
            else None
        )
        return items, next_cursor
