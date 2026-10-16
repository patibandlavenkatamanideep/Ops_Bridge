"""Investigation runs, investigation versions, evidence, corrections and resolution."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.services import investigations as svc

from ..deps import CurrentPrincipal, State
from ..errors import ApiError, conflict, not_found
from .cases import CaseOut, case_out

router = APIRouter(prefix="/api/v1/tenants/{tenant}/cases/{case_id}", tags=["investigations"])


class RunOut(BaseModel):
    run_id: str
    status: str
    model_mode: str
    workflow_version: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    steps: list[dict[str, Any]] = Field(default_factory=list)


class InvestigationOut(BaseModel):
    schema_version: Literal[1] = 1
    id: str
    version: int
    outcome: str
    evidence_readiness: str
    findings: list[dict[str, Any]]
    explanation: str
    explanation_origin: str
    recommendation: dict[str, Any]
    uncertainty: list[str]
    citations: list[dict[str, Any]]
    evidence_visible: bool
    policy_ref: str
    rules_version: str
    source_manifest: list[dict[str, Any]]
    source_manifest_digest: str
    created_at: str
    history: list[dict[str, Any]]
    # Mode requested, data routing from policy, and why a model was or was not used.
    model_routing: dict[str, Any] | None = None


class DocumentVersionOut(BaseModel):
    version_id: str
    version_no: int
    raw_sha256: str
    normalized_sha256: str | None
    normalization_version: str | None
    content_type: str
    page_count: int | None
    parse_status: str
    parse_detail: str | None
    created_at: str


class DocumentOut(BaseModel):
    document_id: str
    title: str
    kind: str
    role: str
    origin: str
    classification: str
    versions: list[DocumentVersionOut]


class DocumentList(BaseModel):
    items: list[DocumentOut]


class CorrectionOut(BaseModel):
    correction_id: str
    previous_value: str | None
    new_value: str | None
    reason: str
    author: str
    created_at: str


class FieldOut(BaseModel):
    field_id: str
    record_type: str
    record_key: str
    field_path: str
    original_value: str | None
    current_value: str | None
    value_type: str
    locator: str
    method: str
    status: str
    note: str | None
    corrections: list[CorrectionOut]


class ChunkOut(BaseModel):
    locator: str
    page: int | None
    line_start: int | None
    line_end: int | None
    text: str


class VersionDetailOut(BaseModel):
    document: dict[str, str]
    version: dict[str, Any]
    fields: list[FieldOut]
    chunks: list[ChunkOut]


class CorrectionCreated(BaseModel):
    correction_id: str
    field_id: str
    previous_value: str | None
    new_value: str


class StartIn(BaseModel):
    model_mode: Literal["rules_only", "mock", "anthropic", "ollama"] | None = None


class CorrectionIn(BaseModel):
    new_value: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=5, max_length=1000)
    expected_current_value: str | None = Field(default=None, max_length=500)


class ResolveIn(BaseModel):
    note: str = Field(default="", max_length=2000)


def _run_out(run: Any, steps: list[Any] | None = None) -> RunOut:
    return RunOut(
        run_id=str(run.id),
        status=run.status,
        model_mode=run.model_mode,
        workflow_version=run.workflow_version,
        created_at=run.created_at.isoformat(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        error=run.error,
        steps=[
            {
                "name": s.step_name,
                "status": s.status,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                "error": s.error,
            }
            for s in (steps or [])
        ],
    )


def _map_errors(exc: Exception) -> ApiError:
    if isinstance(exc, svc.NotFound):
        return not_found(str(exc) or "resource")
    if isinstance(exc, svc.Conflict):
        return conflict(exc.code, str(exc), exc.details)
    raise exc


@router.post("/investigations", response_model=RunOut, status_code=202)
def start(
    case_id: uuid.UUID,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
    response: Response,
    body: StartIn | None = None,
) -> RunOut:
    try:
        run, created = svc.start_investigation(
            state.db,
            principal,
            case_id,
            body.model_mode if body else None,
            request.state.correlation_id,
        )
    except (svc.NotFound, svc.Conflict) as exc:
        raise _map_errors(exc) from exc
    except svc.QuotaExceeded as exc:
        raise ApiError(
            429,
            "quota_exceeded",
            str(exc),
            retryable=True,
            details={"retry_after_seconds": exc.retry_after_seconds},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    if not created:
        response.status_code = 200
    return _run_out(run)


@router.get("/runs/{run_id}", response_model=RunOut)
def run_status(
    case_id: uuid.UUID, run_id: uuid.UUID, principal: CurrentPrincipal, state: State
) -> RunOut:
    try:
        run, steps = svc.get_run(state.db, principal, case_id, run_id)
    except svc.NotFound as exc:
        raise _map_errors(exc) from exc
    return _run_out(run, steps)


@router.get("/investigation", response_model=InvestigationOut)
def latest(
    case_id: uuid.UUID, principal: CurrentPrincipal, state: State, version: int | None = None
) -> InvestigationOut:
    try:
        view = svc.latest_investigation(state.db, principal, case_id, version)
    except svc.NotFound as exc:
        raise _map_errors(exc) from exc
    if view is None:
        raise not_found("investigation")
    iv = view.version
    findings = iv.findings
    if not view.evidence_visible:  # requesters see outcomes, not evidence locations/values
        findings = [
            {k: v for k, v in f.items() if k not in ("evidence", "values")} for f in findings
        ]
    return InvestigationOut(
        id=str(iv.id),
        version=iv.version,
        outcome=iv.outcome,
        evidence_readiness=iv.evidence_readiness,
        findings=findings,
        explanation=iv.explanation,
        explanation_origin=iv.explanation_origin,
        recommendation=iv.recommendation,
        uncertainty=iv.uncertainty,
        citations=view.citations,
        evidence_visible=view.evidence_visible,
        policy_ref=iv.policy_ref,
        rules_version=iv.rules_version,
        source_manifest=iv.source_manifest if view.evidence_visible else [],
        source_manifest_digest=iv.source_manifest_digest,
        created_at=iv.created_at.isoformat(),
        history=view.versions,
        model_routing=view.model_routing,
    )


@router.get("/documents", response_model=DocumentList)
def documents(case_id: uuid.UUID, principal: CurrentPrincipal, state: State) -> dict[str, Any]:
    try:
        return {"items": svc.case_documents(state.db, principal, case_id)}
    except svc.NotFound as exc:
        raise _map_errors(exc) from exc


@router.get("/documents/versions/{version_id}", response_model=VersionDetailOut)
def version(
    case_id: uuid.UUID, version_id: uuid.UUID, principal: CurrentPrincipal, state: State
) -> dict[str, Any]:
    try:
        return svc.version_detail(state.db, principal, case_id, version_id)
    except svc.NotFound as exc:
        raise _map_errors(exc) from exc


@router.get("/documents/versions/{version_id}/original")
def original(
    case_id: uuid.UUID, version_id: uuid.UUID, principal: CurrentPrincipal, state: State
) -> Response:
    try:
        data, ctype, name = svc.raw_document(
            state.db,
            LocalArtifactStore(state.settings.artifact_dir),
            principal,
            case_id,
            version_id,
        )
    except svc.NotFound as exc:
        raise _map_errors(exc) from exc
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "no-store",
        },
    )


@router.post("/fields/{field_id}/corrections", status_code=201, response_model=CorrectionCreated)
def correct(
    case_id: uuid.UUID,
    field_id: uuid.UUID,
    body: CorrectionIn,
    principal: CurrentPrincipal,
    state: State,
    request: Request,
) -> dict[str, Any]:
    try:
        return svc.correct_field(
            state.db,
            principal,
            case_id,
            field_id,
            body.new_value,
            body.reason,
            body.expected_current_value,
            request.state.correlation_id,
        )
    except (svc.NotFound, svc.Conflict) as exc:
        raise _map_errors(exc) from exc


@router.post("/resolve", response_model=CaseOut)
def resolve(
    case_id: uuid.UUID, body: ResolveIn, principal: CurrentPrincipal, state: State, request: Request
) -> CaseOut:
    try:
        case = svc.resolve_case(
            state.db, principal, case_id, body.note, request.state.correlation_id
        )
    except (svc.NotFound, svc.Conflict) as exc:
        raise _map_errors(exc) from exc
    return case_out(case)
