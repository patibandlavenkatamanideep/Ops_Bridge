"""Proposals, approval decisions, operations and their attempt timelines."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from opsbridge.persistence.models import Attempt, Operation, Proposal
from opsbridge.services import proposals as svc

from ..deps import CurrentPrincipal, State
from ..errors import ApiError, conflict, not_found

router = APIRouter(prefix="/api/v1/tenants/{tenant}/cases/{case_id}", tags=["proposals"])


class ProposalOut(BaseModel):
    schema_version: Literal[1] = 1
    id: str
    proposal_key: str
    version: int
    action_type: str
    parameters: dict[str, Any]
    effect_summary: str
    affected_resources: list[str]
    evidence_refs: list[dict[str, Any]]
    source_manifest_digest: str
    investigation_version_id: str
    policy_ref: str
    required_permission: str
    payload_digest: str
    canonicalization: str
    author_kind: str
    author_id: str
    status: str
    generated_at: str
    expires_at: str


class ProposalList(BaseModel):
    items: list[ProposalOut]


class ProposalCreateIn(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=140)
    body: str | None = Field(default=None, max_length=4000)


class ProposalEditIn(BaseModel):
    expected_payload_digest: str = Field(min_length=10, max_length=80)
    title: str | None = Field(default=None, min_length=3, max_length=140)
    body: str | None = Field(default=None, max_length=4000)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    version: int = Field(ge=1)
    payload_digest: str = Field(min_length=10, max_length=80)
    reason: str | None = Field(default=None, max_length=1000)


class AttemptOut(BaseModel):
    id: str
    parent_attempt_id: str | None
    retry_index: int
    kind: str
    tool_name: str
    connector_version: str
    status: str
    error_category: str | None
    error_detail: str | None
    remote_ref: str | None
    input_digest: str
    output_digest: str | None
    worker_id: str | None
    started_at: str
    finished_at: str | None


class OperationOut(BaseModel):
    id: str
    proposal_id: str
    approval_id: str
    operation_key: str
    action_type: str
    connector: str
    payload_digest: str
    status: str
    cancellation: str
    requested_by: str
    approved_by: str
    executed_by: str | None
    remote_ref: str | None
    confirmation: dict[str, Any] | None
    failure_category: str | None
    review_required: bool
    created_at: str
    updated_at: str
    attempts: list[AttemptOut]


class OperationList(BaseModel):
    items: list[OperationOut]


class DecisionOut(BaseModel):
    proposal: ProposalOut
    approval_id: str
    decision: str
    operation: OperationOut | None


class ReviewIn(BaseModel):
    finding: Literal["exists", "absent"]
    remote_ref: str | None = Field(default=None, max_length=200)
    note: str = Field(min_length=5, max_length=500)


def proposal_out(p: Proposal) -> ProposalOut:
    return ProposalOut(
        id=str(p.id),
        proposal_key=str(p.proposal_key),
        version=p.version,
        action_type=p.action_type,
        parameters=p.parameters,
        effect_summary=p.effect_summary,
        affected_resources=p.affected_resources,
        evidence_refs=p.evidence_refs,
        source_manifest_digest=p.source_manifest_digest,
        investigation_version_id=str(p.investigation_version_id),
        policy_ref=p.policy_ref,
        required_permission=p.required_permission,
        payload_digest=p.payload_digest,
        canonicalization=p.canonicalization,
        author_kind=p.author_kind,
        author_id=str(p.author_id),
        status=p.status,
        generated_at=p.generated_at.isoformat(),
        expires_at=p.expires_at.isoformat(),
    )


def attempt_out(a: Attempt) -> AttemptOut:
    return AttemptOut(
        id=str(a.id),
        parent_attempt_id=str(a.parent_attempt_id) if a.parent_attempt_id else None,
        retry_index=a.retry_index,
        kind=a.kind,
        tool_name=a.tool_name,
        connector_version=a.connector_version,
        status=a.status,
        error_category=a.error_category,
        error_detail=a.error_detail,
        remote_ref=a.remote_ref,
        input_digest=a.input_digest,
        output_digest=a.output_digest,
        worker_id=a.worker_id,
        started_at=a.started_at.isoformat(),
        finished_at=a.finished_at.isoformat() if a.finished_at else None,
    )


def operation_out(o: Operation, attempts: list[Attempt]) -> OperationOut:
    return OperationOut(
        id=str(o.id),
        proposal_id=str(o.proposal_id),
        approval_id=str(o.approval_id),
        operation_key=o.operation_key,
        action_type=o.action_type,
        connector=o.connector,
        payload_digest=o.payload_digest,
        status=o.status,
        cancellation=o.cancellation,
        requested_by=str(o.requested_by),
        approved_by=str(o.approved_by),
        executed_by=o.executed_by,
        remote_ref=o.remote_ref,
        confirmation=o.confirmation,
        failure_category=o.failure_category,
        review_required=o.review_required,
        created_at=o.created_at.isoformat(),
        updated_at=o.updated_at.isoformat(),
        attempts=[attempt_out(a) for a in attempts],
    )


def _map(exc: Exception) -> ApiError:
    if isinstance(exc, svc.NotFound):
        return not_found(str(exc) or "resource")
    if isinstance(exc, svc.Conflict):
        return conflict(exc.code, str(exc), exc.details)
    if isinstance(exc, svc.Forbidden):
        return ApiError(403, exc.code, str(exc))
    raise exc


ERRORS = (svc.NotFound, svc.Conflict, svc.Forbidden)


@router.get("/proposals", response_model=ProposalList)
def list_proposals(case_id: uuid.UUID, principal: CurrentPrincipal, state: State) -> ProposalList:
    try:
        return ProposalList(
            items=[proposal_out(p) for p in svc.list_proposals(state.db, principal, case_id)]
        )
    except ERRORS as exc:
        raise _map(exc) from exc


@router.post("/proposals", response_model=ProposalOut, status_code=201)
def create_proposal(
    case_id: uuid.UUID,
    body: ProposalCreateIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> ProposalOut:
    overrides = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return proposal_out(
            svc.create_from_investigation(
                state.db, principal, case_id, overrides, request.state.correlation_id
            )
        )
    except ERRORS as exc:
        raise _map(exc) from exc


@router.put("/proposals/{proposal_id}", response_model=ProposalOut, status_code=201)
def edit_proposal(
    case_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: ProposalEditIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> ProposalOut:
    changes = {
        k: v
        for k, v in body.model_dump(exclude={"expected_payload_digest"}).items()
        if v is not None
    }
    try:
        return proposal_out(
            svc.edit(
                state.db,
                principal,
                case_id,
                proposal_id,
                body.expected_payload_digest,
                changes,
                request.state.correlation_id,
            )
        )
    except ERRORS as exc:
        raise _map(exc) from exc


@router.post("/proposals/{proposal_id}/decision", response_model=DecisionOut)
def decide(
    case_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: DecisionIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> DecisionOut:
    try:
        pr, ad, op = svc.decide(
            state.db,
            principal,
            case_id,
            proposal_id,
            decision=body.decision,
            version=body.version,
            payload_digest=body.payload_digest,
            reason=body.reason,
            correlation_id=request.state.correlation_id,
        )
    except ERRORS as exc:
        raise _map(exc) from exc
    return DecisionOut(
        proposal=proposal_out(pr),
        approval_id=str(ad.id),
        decision=ad.decision,
        operation=operation_out(op, []) if op else None,
    )


@router.get("/operations", response_model=OperationList)
def list_operations(case_id: uuid.UUID, principal: CurrentPrincipal, state: State) -> OperationList:
    try:
        return OperationList(
            items=[
                operation_out(o, a) for o, a in svc.list_operations(state.db, principal, case_id)
            ]
        )
    except ERRORS as exc:
        raise _map(exc) from exc


@router.post("/operations/{operation_id}/cancel", response_model=OperationOut)
def cancel(
    case_id: uuid.UUID,
    operation_id: uuid.UUID,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> OperationOut:
    try:
        return operation_out(
            svc.request_cancellation(
                state.db, principal, case_id, operation_id, request.state.correlation_id
            ),
            [],
        )
    except ERRORS as exc:
        raise _map(exc) from exc


@router.post("/operations/{operation_id}/review", response_model=OperationOut)
def review(
    case_id: uuid.UUID,
    operation_id: uuid.UUID,
    body: ReviewIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> OperationOut:
    try:
        return operation_out(
            svc.review_unknown_outcome(
                state.db,
                principal,
                case_id,
                operation_id,
                finding=body.finding,
                remote_ref=body.remote_ref,
                note=body.note,
                correlation_id=request.state.correlation_id,
            ),
            [],
        )
    except ERRORS as exc:
        raise _map(exc) from exc
