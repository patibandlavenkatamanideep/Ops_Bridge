"""Case intake, queue and detail endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field

from opsbridge.persistence.models import Case
from opsbridge.services import cases as svc

from ..deps import CurrentPrincipal, State
from ..errors import conflict, invalid, not_found

router = APIRouter(prefix="/api/v1/tenants/{tenant}", tags=["cases"])


class CaseCreateIn(BaseModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=3, max_length=300)
    description: str = Field(default="", max_length=5000)
    invoice_number: str | None = Field(default=None, max_length=100)
    po_number: str | None = Field(default=None, max_length=100)
    supplier_id: str | None = Field(default=None, max_length=100)
    # Set after checking that a similar-looking invoice number is a different document.
    confirm_distinct: bool = False


class CaseOut(BaseModel):
    schema_version: Literal[1] = 1
    id: str
    case_number: str
    title: str
    description: str
    invoice_number: str | None
    po_number: str | None
    supplier_id: str | None
    status: str
    discrepancy_types: list[str]
    evidence_readiness: str
    requester_id: str
    assignee_id: str | None
    owner: str | None = None  # the owner's display name
    assignee_group: str | None
    created_at: str
    updated_at: str
    row_version: int


class QueueItemOut(BaseModel):
    id: str
    case_number: str
    title: str
    status: str
    discrepancy_types: list[str]
    evidence_readiness: str
    owner: str | None
    owner_id: str | None = None
    owner_active: bool = True  # False: the owner no longer works here as an investigator
    assignee_group: str | None
    # What waits for someone: a proposal for a decision, an operation for a human check.
    awaiting_approval: bool = False
    needs_review: bool = False
    age_seconds: int
    created_at: str


class QueuePage(BaseModel):
    items: list[QueueItemOut]
    next_cursor: str | None


class QueueViewCounts(BaseModel):
    mine: int
    unassigned: int
    awaiting_approval: int
    needs_review: int


class OwnerIn(BaseModel):
    owner_id: uuid.UUID | None  # None releases the case
    expected_owner_id: uuid.UUID | None  # the owner the caller saw


class AssigneeOut(BaseModel):
    id: str
    username: str
    display_name: str


class AssigneeList(BaseModel):
    items: list[AssigneeOut]


def case_out(c: Case, owner: str | None = None) -> CaseOut:
    return CaseOut(
        id=str(c.id),
        case_number=c.case_number,
        title=c.title,
        description=c.description,
        invoice_number=c.invoice_number,
        po_number=c.po_number,
        supplier_id=c.supplier_id,
        status=c.status,
        discrepancy_types=list(c.discrepancy_types or []),
        evidence_readiness=c.evidence_readiness,
        requester_id=str(c.requester_id),
        assignee_id=str(c.assignee_id) if c.assignee_id else None,
        owner=owner,
        assignee_group=c.assignee_group,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
        row_version=c.row_version,
    )


@router.post("/cases", response_model=CaseOut, status_code=201)
def create_case(
    body: CaseCreateIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=100)] = None,
) -> CaseOut:
    if idempotency_key is not None and not idempotency_key.strip():
        raise invalid("Idempotency-Key must not be blank.")
    try:
        case, created = svc.create_case(
            state.db,
            principal,
            svc.CaseCreate(
                body.title,
                body.description,
                body.invoice_number,
                body.po_number,
                body.supplier_id,
                body.confirm_distinct,
            ),
            idempotency_key,
            correlation_id=request.state.correlation_id,
        )
    except svc.DuplicateCase as exc:
        raise conflict(
            "duplicate_case",
            "A case already exists for this invoice.",
            {"existing_case_id": str(exc.existing_id)} if exc.existing_id else None,
        ) from exc
    except svc.PossibleDuplicate as exc:
        raise conflict(
            "possible_duplicate",
            "A case exists for an invoice number that differs only in punctuation, spacing or "
            "leading zeros. If it is a different invoice, submit again with confirm_distinct.",
            {
                "existing_case_id": str(exc.existing_id),
                "existing_invoice_number": exc.existing_invoice_number,
            }
            if exc.existing_id
            else None,
        ) from exc
    except svc.IdempotencyKeyReused as exc:
        raise conflict(
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request.",
        ) from exc
    if not created:
        response.status_code = 200
    return case_out(case)


@router.get("/cases", response_model=QueuePage)
def list_cases(
    principal: CurrentPrincipal,
    state: State,
    status: Annotated[str | None, Query(max_length=40)] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    view: Annotated[str, Query(max_length=32)] = "all",
    sort: Annotated[str, Query(max_length=16)] = "newest",
) -> QueuePage:
    if status is not None and status not in svc.CASE_STATUSES:
        raise invalid("Unknown status filter.", {"allowed": list(svc.CASE_STATUSES)})
    if view not in svc.QUEUE_VIEWS:
        raise invalid("Unknown queue view.", {"allowed": list(svc.QUEUE_VIEWS)})
    if sort not in svc.QUEUE_SORTS:
        raise invalid("Unknown sort order.", {"allowed": list(svc.QUEUE_SORTS)})
    try:
        items, next_cursor = svc.list_cases(
            state.db, principal, status=status, cursor=cursor, limit=limit, search=q,
            view=view, sort=sort,
        )  # fmt: skip
    except (ValueError, TypeError) as exc:
        raise invalid("Invalid cursor.") from exc
    now = datetime.now(UTC)
    return QueuePage(
        items=[
            QueueItemOut(
                id=str(i.case.id),
                case_number=i.case.case_number,
                title=i.case.title,
                status=i.case.status,
                discrepancy_types=list(i.case.discrepancy_types or []),
                evidence_readiness=i.case.evidence_readiness,
                owner=i.assignee_name,
                owner_id=str(i.case.assignee_id) if i.case.assignee_id else None,
                owner_active=i.owner_active,
                assignee_group=i.case.assignee_group,
                awaiting_approval=i.awaiting_approval,
                needs_review=i.needs_review,
                age_seconds=int((now - i.case.created_at).total_seconds()),
                created_at=i.case.created_at.isoformat(),
            )
            for i in items
        ],
        next_cursor=next_cursor,
    )


@router.get("/cases/views", response_model=QueueViewCounts)
def queue_view_counts(principal: CurrentPrincipal, state: State) -> QueueViewCounts:
    """How many cases each built-in queue view holds for the caller."""
    return QueueViewCounts.model_validate(svc.view_counts(state.db, principal))


@router.get("/assignees", response_model=AssigneeList)
def assignees(principal: CurrentPrincipal, state: State) -> AssigneeList:
    """Who a case can be given to (active investigators)."""
    return AssigneeList(
        items=[AssigneeOut(**m) for m in svc.assignable_members(state.db, principal)]
    )


@router.get("/cases/{case_id}", response_model=CaseOut)
def get_case(case_id: uuid.UUID, principal: CurrentPrincipal, state: State) -> CaseOut:
    case = svc.get_case(state.db, principal, case_id)
    if case is None:
        raise not_found("case")
    return case_out(case, svc.owner_name(state.db, principal, case.assignee_id))


@router.post("/cases/{case_id}/owner", response_model=CaseOut)
def set_owner(
    case_id: uuid.UUID, body: OwnerIn, principal: CurrentPrincipal, state: State, request: Request
) -> CaseOut:
    """Take (owner_id = yourself), assign or release (owner_id = null) a case."""
    try:
        case = svc.set_owner(
            state.db, principal, case_id, owner_id=body.owner_id,
            expected_owner_id=body.expected_owner_id, correlation_id=request.state.correlation_id,
        )  # fmt: skip
    except LookupError as exc:
        raise not_found("case") from exc
    except svc.OwnerChanged as exc:
        raise conflict(
            "owner_changed",
            "Someone else changed who owns this case; reload and try again.",
            {"current_owner_id": str(exc.current) if exc.current else None},
        ) from exc
    except svc.NotAssignable as exc:
        raise conflict("not_assignable", str(exc)) from exc
    return case_out(case, svc.owner_name(state.db, principal, case.assignee_id))
