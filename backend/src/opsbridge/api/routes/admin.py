"""Tenant settings, retention, uploads and case conversations."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, Field

from opsbridge.auth.permissions import require
from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.packs import PackRegistry
from opsbridge.persistence.db import DbContext
from opsbridge.services import casework_admin as svc
from opsbridge.services.investigations import Conflict, NotFound
from opsbridge.workflow import jobs

from ..deps import CurrentPrincipal, State
from ..errors import ApiError, conflict, not_found

tenant_router = APIRouter(prefix="/api/v1/tenants/{tenant}", tags=["tenant"])
case_router = APIRouter(prefix="/api/v1/tenants/{tenant}/cases/{case_id}", tags=["case-extras"])


def _map(exc: Exception) -> ApiError:
    if isinstance(exc, NotFound):
        return not_found(str(exc) or "resource")
    if isinstance(exc, Conflict):
        return conflict(exc.code, str(exc), exc.details)
    raise exc


class SettingsIn(BaseModel):
    model_mode: str | None = Field(default=None, max_length=24)
    task_connector: str | None = Field(default=None, max_length=32)
    retention: dict[str, int] | None = None


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
    username: str
    display_name: str | None
    roles: list[str]
    approval_scope: list[str]
    status: str


class SettingsOut(BaseModel):
    tenant: TenantInfo
    entitlements: Entitlements
    settings: SettingsValues
    members: list[MemberOut]
    retention_keys: list[str] = list(svc.RETENTION_KEYS)


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


@tenant_router.put("/settings", response_model=SettingsOut)
def put_settings(
    body: SettingsIn, principal: CurrentPrincipal, state: State, request: Request
) -> SettingsOut:
    try:
        view = svc.update_settings(
            state.db,
            principal,
            model_mode=body.model_mode,
            task_connector=body.task_connector,
            retention=body.retention,
            correlation_id=request.state.correlation_id,
        )
    except (NotFound, Conflict) as exc:
        raise _map(exc) from exc
    return SettingsOut.model_validate(view)


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
            LocalArtifactStore(state.settings.artifact_dir),
            pack,
            principal,
            case_id,
            kind=kind,
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            data=data,
            correlation_id=request.state.correlation_id,
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
