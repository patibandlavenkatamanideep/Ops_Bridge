"""Tenant settings, members and invitations, retention, uploads and case conversations."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from opsbridge.auth.permissions import Role, require
from opsbridge.domain.policy import ActionType
from opsbridge.evidence.parsers import OcrSettings
from opsbridge.evidence.storage import artifact_store
from opsbridge.packs import PackRegistry
from opsbridge.persistence.db import DbContext
from opsbridge.services import casework_admin as svc
from opsbridge.services import connections as conn_svc
from opsbridge.services import invitations as inv_svc
from opsbridge.services.investigations import Conflict, NotFound
from opsbridge.workflow import jobs

from ..deps import CurrentPrincipal, CurrentSession, State
from ..errors import ApiError, conflict, not_found

tenant_router = APIRouter(prefix="/api/v1/tenants/{tenant}", tags=["tenant"])
# Accepting an invitation happens before the person belongs to the tenant: no tenant path.
invitation_router = APIRouter(prefix="/api/v1/invitations", tags=["invitations"])
case_router = APIRouter(prefix="/api/v1/tenants/{tenant}/cases/{case_id}", tags=["case-extras"])


def _map(exc: Exception) -> ApiError:
    if isinstance(exc, NotFound):
        return not_found(str(exc) or "resource")
    if isinstance(exc, Conflict):
        return conflict(exc.code, str(exc), exc.details)
    raise exc


class SettingsIn(BaseModel):
    # Unknown fields are refused: the task connector moved to /task-connection, and a client
    # still sending it must not be told its change was saved.
    model_config = ConfigDict(extra="forbid")
    model_mode: str | None = Field(default=None, max_length=24)
    retention: dict[str, int] | None = None


class TaskConnectionOut(BaseModel):
    id: str
    version: int
    connector: str
    destination: dict[str, str]
    credential_ref: str | None
    credential_source: str
    credential_configured: bool
    status: Literal["active", "superseded", "revoked"]
    reason: str | None
    created_at: str
    created_by: str | None
    ended_at: str | None
    ended_by: str | None
    end_reason: str | None


class TaskConnectionsOut(BaseModel):
    active: TaskConnectionOut | None
    versions: list[TaskConnectionOut]
    allowed_repositories: list[str]  # GitHub repositories this deployment allows


class TaskConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector: Literal["sim_tasks", "github_issues"]
    repository: str | None = Field(default=None, max_length=201)
    credential_ref: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=500)


class RevokeConnectionIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class TenantInfo(BaseModel):
    slug: str
    name: str
    tier: str
    pack_id: str
    synthetic_demo: bool


class Entitlements(BaseModel):
    model_modes: list[str]
    external_connectors: list[str]
    local_inference: bool
    max_model_calls_per_run: int


class SettingsValues(BaseModel):
    model_mode: str
    task_connector: str
    retention: dict[str, int]


class MemberOut(BaseModel):
    id: str
    username: str
    display_name: str | None
    roles: list[str]
    approval_scope: list[str]
    status: str
    change_reason: str | None  # the latest in-app change; seeded memberships have none
    changed_at: str | None


class MemberUpdateIn(BaseModel):
    """Fields left out stay as they are; dropping the approver role clears the scope."""

    roles: list[Role] | None = Field(default=None, max_length=5)
    approval_scope: list[ActionType] | None = Field(default=None, max_length=10)
    status: Literal["active", "revoked"] | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)]


class InvitationOut(BaseModel):
    id: str
    email: str
    roles: list[str]
    approval_scope: list[str]
    invited_by: str | None
    created_at: str
    expires_at: str
    expired: bool


class InvitationIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    roles: list[Role] = Field(min_length=1, max_length=5)
    approval_scope: list[ActionType] = Field(default_factory=list, max_length=10)


class AcceptIn(BaseModel):
    token: str = Field(min_length=20, max_length=100)


class AcceptOut(BaseModel):
    tenant_slug: str
    tenant_name: str
    roles: list[str]


class PolicyOut(BaseModel):
    policy_id: str
    version: int
    effective_from: str
    sha256: str
    state: str  # active | superseded | scheduled


class AttemptStat(BaseModel):
    kind: str
    status: str
    count: int
    last_at: str | None


class ConnectorHealthOut(BaseModel):
    connector: str  # "none" when the connection was revoked
    connection: TaskConnectionOut | None = None
    checked_at: str
    reachable: bool | None  # None: not checked live (needs authorization)
    capabilities: dict[str, Any]
    capability_source: str
    meaning: str
    configuration: dict[str, bool]
    attempts_24h: list[AttemptStat]
    awaiting_review: int


class SettingsOut(BaseModel):
    tenant: TenantInfo
    entitlements: Entitlements
    settings: SettingsValues
    policies: list[PolicyOut]
    members: list[MemberOut]
    invitations: list[InvitationOut]
    retention_keys: list[str] = list(svc.RETENTION_KEYS)


class InvitationCreatedOut(BaseModel):
    invitation_id: str
    token: str  # returned once; only its SHA-256 is stored
    expires_at: str
    settings: SettingsOut


class UploadOut(BaseModel):
    document_id: str
    version_id: str
    version_no: int
    raw_sha256: str
    parse_status: str
    parse_detail: str | None
    problems: list[str]
    records: dict[str, int]


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    supersedes_id: uuid.UUID | None = None


class DeleteIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


class MessageOut(BaseModel):
    id: str
    seq: int
    author_kind: str
    author_id: str | None
    author: str | None
    body: str | None
    body_sha256: str
    supersedes_id: str | None
    created_at: str
    deleted_at: str | None
    deletion_reason: str | None


class MessageList(BaseModel):
    items: list[MessageOut]


def _msg(m: Any, author: str | None) -> MessageOut:
    return MessageOut(
        id=str(m.id),
        seq=m.seq,
        author_kind=m.author_kind,
        author_id=str(m.author_id) if m.author_id else None,
        author=author,
        body=m.body,
        body_sha256=m.body_sha256,
        supersedes_id=str(m.supersedes_id) if m.supersedes_id else None,
        created_at=m.created_at.isoformat(),
        deleted_at=m.deleted_at.isoformat() if m.deleted_at else None,
        deletion_reason=m.deletion_reason,
    )


@tenant_router.get("/settings", response_model=SettingsOut)
def get_settings(principal: CurrentPrincipal, state: State) -> SettingsOut:
    return SettingsOut.model_validate(svc.get_settings_view(state.db, principal))


@tenant_router.get("/connectors/health", response_model=ConnectorHealthOut)
def connector_health(principal: CurrentPrincipal, state: State) -> ConnectorHealthOut:
    try:
        return ConnectorHealthOut.model_validate(
            svc.connector_health(state.db, principal, state.settings)
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc


@tenant_router.get("/task-connection", response_model=TaskConnectionsOut)
def task_connection(principal: CurrentPrincipal, state: State) -> TaskConnectionsOut:
    return TaskConnectionsOut.model_validate(conn_svc.history(state.db, principal, state.settings))


@tenant_router.post("/task-connection", response_model=TaskConnectionOut, status_code=201)
def change_task_connection(
    body: TaskConnectionIn, principal: CurrentPrincipal, state: State, request: Request
) -> TaskConnectionOut:
    """A new version of the tenant's task connection. Pending approvals for another
    destination become stale; nothing already sent is redirected."""
    try:
        return TaskConnectionOut.model_validate(
            conn_svc.change(
                state.db,
                principal,
                state.settings,
                connector=body.connector,
                repository=body.repository,
                credential_ref=body.credential_ref,
                reason=body.reason,
                correlation_id=request.state.correlation_id,
            )
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc


@tenant_router.post("/task-connection/revoke", response_model=TaskConnectionOut)
def revoke_task_connection(
    body: RevokeConnectionIn, principal: CurrentPrincipal, state: State, request: Request
) -> TaskConnectionOut:
    """Stop sending follow-up tasks until a new connection is made."""
    try:
        return TaskConnectionOut.model_validate(
            conn_svc.revoke(
                state.db,
                principal,
                state.settings,
                reason=body.reason,
                correlation_id=request.state.correlation_id,
            )
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc


@tenant_router.put("/settings", response_model=SettingsOut)
def put_settings(
    body: SettingsIn, principal: CurrentPrincipal, state: State, request: Request
) -> SettingsOut:
    try:
        view = svc.update_settings(
            state.db,
            principal,
            model_mode=body.model_mode,
            retention=body.retention,
            correlation_id=request.state.correlation_id,
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return SettingsOut.model_validate(view)


@tenant_router.patch("/members/{membership_id}", response_model=SettingsOut)
def update_member(
    membership_id: uuid.UUID,
    body: MemberUpdateIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> SettingsOut:
    try:
        view = svc.update_membership(
            state.db,
            principal,
            membership_id,
            roles=list(body.roles) if body.roles is not None else None,
            approval_scope=list(body.approval_scope) if body.approval_scope is not None else None,
            status=body.status,
            reason=body.reason,
            correlation_id=request.state.correlation_id,
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return SettingsOut.model_validate(view)


@tenant_router.post("/invitations", status_code=201, response_model=InvitationCreatedOut)
def create_invitation(
    body: InvitationIn, principal: CurrentPrincipal, state: State, request: Request
) -> InvitationCreatedOut:
    try:
        view, token, inv = inv_svc.create_invitation(
            state.db,
            principal,
            email=body.email,
            roles=list(body.roles),
            approval_scope=list(body.approval_scope),
            ttl_hours=state.settings.invitation_ttl_hours,
            max_open=state.settings.max_open_invitations,
            correlation_id=request.state.correlation_id,
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return InvitationCreatedOut(
        invitation_id=str(inv.id),
        token=token,
        expires_at=inv.expires_at.isoformat(),
        settings=SettingsOut.model_validate(view),
    )


@tenant_router.post("/invitations/{invitation_id}/revoke", response_model=SettingsOut)
def revoke_invitation(
    invitation_id: uuid.UUID, principal: CurrentPrincipal, state: State, request: Request
) -> SettingsOut:
    try:
        view = inv_svc.revoke_invitation(
            state.db, principal, invitation_id, request.state.correlation_id
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return SettingsOut.model_validate(view)


@invitation_router.post("/accept", response_model=AcceptOut)
def accept_invitation(
    body: AcceptIn, info: CurrentSession, state: State, request: Request
) -> AcceptOut:
    try:
        joined = inv_svc.accept_invitation(
            state.db, info.user_id, body.token, request.state.correlation_id
        )
    except inv_svc.NotForThisAccount as exc:
        raise ApiError(403, exc.code, str(exc)) from exc
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return AcceptOut.model_validate(joined)


@tenant_router.post("/retention/run", status_code=202)
def run_retention(principal: CurrentPrincipal, state: State) -> dict[str, str]:
    require(principal, "tenant.manage_policy")
    with state.db.session(DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)) as s:
        job = jobs.enqueue(
            s,
            tenant_id=principal.tenant_id,
            kind="retention_sweep",
            payload={"requested_by": str(principal.user_id)},
        )
        return {"job_id": str(job.id), "status": "queued"}


@case_router.post("/documents", status_code=201, response_model=UploadOut)
async def upload(
    case_id: uuid.UUID,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
    kind: str = Form(..., max_length=32),
    file: UploadFile = File(...),
) -> UploadOut:
    limit = state.settings.max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ApiError(413, "payload_too_large", f"Uploads are limited to {limit} bytes.")
    pack = PackRegistry(state.settings.packs_dir).get(principal.tenant_pack)
    from starlette.concurrency import run_in_threadpool

    try:
        result = await run_in_threadpool(
            svc.upload_document,
            state.db,
            artifact_store(state.settings),
            pack,
            principal,
            case_id,
            kind=kind,
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            data=data,
            correlation_id=request.state.correlation_id,
            ocr=OcrSettings.from_settings(state.settings),
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return UploadOut.model_validate(result)


@case_router.get("/messages", response_model=MessageList)
def messages(case_id: uuid.UUID, principal: CurrentPrincipal, state: State) -> MessageList:
    try:
        return MessageList(
            items=[_msg(m, a) for m, a in svc.list_messages(state.db, principal, case_id)]
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc


@case_router.post("/messages", response_model=MessageOut, status_code=201)
def post_message(
    case_id: uuid.UUID, body: MessageIn, principal: CurrentPrincipal, state: State, request: Request
) -> MessageOut:
    try:
        m = svc.post_message(
            state.db,
            principal,
            case_id,
            body.body,
            body.supersedes_id,
            request.state.correlation_id,
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return _msg(m, principal.display_name)


@case_router.post("/messages/{message_id}/delete", response_model=MessageOut)
def delete_message(
    case_id: uuid.UUID,
    message_id: uuid.UUID,
    body: DeleteIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> MessageOut:
    try:
        m = svc.delete_message(
            state.db, principal, case_id, message_id, body.reason, request.state.correlation_id
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return _msg(m, None)
