"""Casework MCP: approved follow-up task operations, read-back and reconciliation.

`create_follow_up_task` takes only an operation id and the executor's attempt id. What is
sent is the outbound text and destination sealed in the approved payload (the same request
the executor would build), so a caller (or a model) cannot substitute its own parameters,
and a different redactor or setting here cannot change the approved bytes.

A create is admitted when the request reaches this tool, with the executor's own admission
function (services/execution_admission.py): cancellation, proposal and approval state,
approval expiry, the approver's current authority, evidence and policy freshness, the
approved destination and text. A request can arrive long after the executor checked. On top:
- caller service identity is an allowed executor (`opsbridge-worker`);
- the delegation names this operation, this attempt and the approver as principal;
- the principal still holds `proposal.decide` with the action in scope;
- the operation is `executing` and its payload digest matches the approved proposal;
- the attempt is the executor's open create for this operation and is admitted once
  (`attempts.admitted_at`), so a replayed or retransmitted request cannot write again.

Read-back and reconciliation recover a write already admitted; they are not new writes.
They need the delegation for the operation and an admitted create, but not the approver's
current authority: revoking the approver must not hide a task that was already sent.

Business outcomes are returned as data (`outcome` + `category`) so the executor can apply
its definite/ambiguous rules; authorization and admission failures are tool errors.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from sqlalchemy import select, text
from starlette.requests import Request
from starlette.responses import JSONResponse

from opsbridge.canonical import digest
from opsbridge.connectors.tasks import (
    AmbiguousFailure,
    DefiniteFailure,
    LookupUnavailable,
    TaskConnector,
)
from opsbridge.persistence.db import Database
from opsbridge.persistence.models import ApprovalDecision, Attempt, Membership, Operation, Proposal
from opsbridge.services import execution_admission as admission

from .auth import DelegationVerifier
from .client import NOT_ADMITTED
from .common import CallContext, Guard, ToolDenied, as_uuid

TOOL_VERSION = "casework/1"
TOOLS = ("create_follow_up_task", "get_follow_up_task", "reconcile_operation")
EXECUTOR_CLIENTS = frozenset({"opsbridge-worker"})


def build_casework_server(
    db: Database,
    token_verifier: Any,
    delegation_verifier: DelegationVerifier,
    resource_url: str,
    issuer_url: str,
    connector_for: Callable[[uuid.UUID, dict[str, Any]], TaskConnector],
    timeout_seconds: float = 5.0,
) -> MCPServer:
    server = MCPServer(
        name="opsbridge-casework",
        version=TOOL_VERSION,
        instructions="Executes approved follow-up operations only. Parameters come from approved records.",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_url,
            required_scopes=[],
            # Audience is enforced by the TokenVerifier (Keycloak audiences are client ids, not
            # URLs), so the SDK's URL-based resource check is explicitly disabled, not left implicit.
            validate_token_resource=False,
        ),
    )
    guard = Guard(db, delegation_verifier, "casework")

    def load(
        cc: CallContext, operation_id: str, *, need_executing: bool
    ) -> tuple[Operation, TaskConnector]:
        """The operation, checked for this caller, and a connector for its APPROVED
        destination. `need_executing` is a create; otherwise recovery of an admitted one."""
        op_id = as_uuid(operation_id, "operation id")
        if cc.service_client not in EXECUTOR_CLIENTS:
            raise ToolDenied(f"service {cc.service_client} may not execute casework operations")
        if cc.delegation.operation_id != op_id:
            raise ToolDenied("delegation does not cover this operation")
        with db.session(cc.db_ctx) as s:
            op = s.get(Operation, op_id)
            if op is None:
                raise ToolDenied("operation not found in the delegated tenant")
            if op.approved_by != cc.delegation.principal_id:
                raise ToolDenied("delegated principal is not the approver of this operation")
            if need_executing:
                m = s.execute(
                    select(Membership).where(
                        Membership.user_id == cc.delegation.principal_id,
                        Membership.status == "active",
                    )
                ).scalar_one_or_none()
                if m is None or op.action_type not in (m.approval_scope or []):
                    raise ToolDenied("approver's scope no longer covers this action")
                pr = s.get(Proposal, op.proposal_id)
                ad = s.get(ApprovalDecision, op.approval_id)
                if op.status != "executing":
                    raise ToolDenied(f"operation is {op.status}, not executing")
                if (
                    pr is None
                    or ad is None
                    or pr.payload_digest != op.payload_digest
                    or ad.payload_digest != op.payload_digest
                    or digest(op.payload) != op.payload_digest
                ):
                    raise ToolDenied("operation payload is not bound to its approval")
            elif (
                s.execute(
                    select(Attempt.id).where(
                        Attempt.operation_id == op.id,
                        Attempt.kind == "dispatch",
                        Attempt.admitted_at.is_not(None),
                    )
                ).first()
                is None
            ):
                # Nothing was ever sent through this service, so there is nothing to recover;
                # without that, reading tasks needs the approver's current authority.
                raise ToolDenied("no create was admitted for this operation")
            s.expunge(op)
        try:
            return op, connector_for(cc.delegation.tenant_id, op.payload.get("destination") or {})
        except ValueError as exc:
            raise ToolDenied(str(exc)) from exc

    def admit(cc: CallContext, op: Operation, attempt_id: str | None) -> None:
        """Admit this create once, with the executor's own admission, at this moment."""
        att = as_uuid(attempt_id or "", "attempt id")
        if cc.delegation.attempt_id != att:
            raise ToolDenied(f"{NOT_ADMITTED}: the delegation does not name this attempt")
        with db.session(cc.db_ctx) as s:
            current = s.get(Operation, op.id)
            assert current is not None
            refused = admission.refusal(s, current, lock_case=False)
            if refused is not None:
                raise ToolDenied(f"{NOT_ADMITTED} ({refused.category}): {refused.detail}")
            claimed = s.execute(
                text(
                    "UPDATE attempts SET admitted_at = now() WHERE id = :a AND operation_id = :o "
                    "AND kind = 'dispatch' AND status = 'started' AND admitted_at IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM attempts later WHERE later.operation_id = :o "
                    "AND later.started_at > attempts.started_at) RETURNING id"
                ),
                {"a": att, "o": op.id},
            ).first()
            if claimed is None:
                raise ToolDenied(
                    f"{NOT_ADMITTED}: attempt {att} is not the executor's open create for this "
                    "operation, or was already admitted"
                )

    @server.tool(
        description="Create the approved follow-up task for an operation (parameters from the record)."
    )
    async def create_follow_up_task(
        operation_id: str,
        ctx: Context,
        deadline: float | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """`deadline` (epoch seconds) is the caller's: the task must not be committed after it,
        so the caller can later prove absence by lookup. `attempt_id` is the executor's open
        create; it is admitted once."""
        cc = guard.authorize(ctx, "create_follow_up_task", "proposal.decide")

        def go() -> dict[str, Any]:
            op, conn = load(cc, operation_id, need_executing=True)
            admit(cc, op, attempt_id)
            req = admission.approved_request(op.payload, op.operation_key, deadline=deadline)
            key = op.operation_key if conn.capabilities.idempotency_keys else None
            try:
                t = conn.create(req, key, timeout_seconds)
            except DefiniteFailure as exc:
                return {
                    "outcome": "definite_failure",
                    "retryable": exc.retryable,
                    "detail": str(exc),
                }
            except AmbiguousFailure as exc:
                return {"outcome": "ambiguous_failure", "detail": str(exc)}
            return {
                "outcome": "created",
                "remote_ref": t.remote_ref,
                "url": t.url,
                "marker": t.marker,
                "title": t.title,
                "connector": conn.version,
            }

        out = await anyio.to_thread.run_sync(go)
        guard.audit_ok(cc, "create_follow_up_task", operation_id)
        return out

    @server.tool(description="Read back a follow-up task created for an operation.")
    async def get_follow_up_task(
        operation_id: str, remote_ref: str, ctx: Context
    ) -> dict[str, Any]:
        cc = guard.authorize(ctx, "get_follow_up_task", None)  # recovery: see load()

        def go() -> dict[str, Any]:
            _op, conn = load(cc, operation_id, need_executing=False)
            try:
                t = conn.get(remote_ref[:200], timeout_seconds)
            except LookupUnavailable as exc:
                return {"outcome": "lookup_unavailable", "detail": str(exc)}
            if t is None:
                return {"outcome": "not_found"}
            return {
                "outcome": "found",
                "remote_ref": t.remote_ref,
                "url": t.url,
                "marker": t.marker,
                "title": t.title,
            }

        out = await anyio.to_thread.run_sync(go)
        guard.audit_ok(cc, "get_follow_up_task", operation_id)
        return out

    @server.tool(
        description="Find tasks carrying an operation's marker (reconciliation after ambiguity)."
    )
    async def reconcile_operation(operation_id: str, ctx: Context) -> dict[str, Any]:
        cc = guard.authorize(ctx, "reconcile_operation", None)  # recovery: see load()

        def go() -> dict[str, Any]:
            op, conn = load(cc, operation_id, need_executing=False)
            try:
                found = conn.find_by_marker(admission.marker(op.operation_key), timeout_seconds)
            except LookupUnavailable as exc:
                return {
                    "outcome": "lookup_unavailable",
                    "detail": str(exc),
                    "lookup": conn.capabilities.lookup,
                }
            return {
                "outcome": "searched",
                "lookup": conn.capabilities.lookup,
                "matches": [
                    {"remote_ref": t.remote_ref, "url": t.url, "marker": t.marker, "title": t.title}
                    for t in found
                ],
            }

        out = await anyio.to_thread.run_sync(go)
        guard.audit_ok(cc, "reconcile_operation", operation_id)
        return out

    @server.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "casework-mcp", "tools": list(TOOLS)})

    return server
