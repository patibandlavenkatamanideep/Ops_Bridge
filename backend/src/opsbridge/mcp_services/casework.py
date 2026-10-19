"""Casework MCP: approved follow-up task operations, read-back and reconciliation.

`create_follow_up_task` takes only an operation id. Title, body, assignee group and the
idempotency key are loaded from the approved operation record, so a caller (or a model)
cannot substitute its own parameters. Additional checks, on top of the executor's own:
- caller service identity is an allowed executor (`opsbridge-worker`);
- the delegation names this operation and the approver as principal;
- the principal still holds `proposal.decide` with the action in scope;
- the operation is `executing`, its payload digest matches the approved proposal, and
  its approval is unexpired.
Business outcomes are returned as data (`outcome` + `category`) so the executor can apply
its definite/ambiguous rules; authorization failures are tool errors.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse

from opsbridge.canonical import digest
from opsbridge.connectors.tasks import (
    AmbiguousFailure,
    DefiniteFailure,
    LookupUnavailable,
    TaskConnector,
    TaskRequest,
)
from opsbridge.evidence.records import case_identifiers
from opsbridge.persistence.db import Database
from opsbridge.persistence.models import ApprovalDecision, Membership, Operation, Proposal
from opsbridge.privacy.redaction import redact

from .auth import DelegationVerifier
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
    connector_for: Callable[[uuid.UUID, str], TaskConnector],
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
            m = s.execute(
                select(Membership).where(
                    Membership.user_id == cc.delegation.principal_id, Membership.status == "active"
                )
            ).scalar_one_or_none()
            if m is None or op.action_type not in (m.approval_scope or []):
                raise ToolDenied("approver's scope no longer covers this action")
            if need_executing:
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
            s.expunge(op)
        return op, connector_for(cc.delegation.tenant_id, op.connector)

    @server.tool(
        description="Create the approved follow-up task for an operation (parameters from the record)."
    )
    async def create_follow_up_task(operation_id: str, ctx: Context) -> dict[str, Any]:
        cc = guard.authorize(ctx, "create_follow_up_task", "proposal.decide")

        def go() -> dict[str, Any]:
            op, conn = load(cc, operation_id, need_executing=True)
            with db.session(cc.db_ctx) as s:
                keep = case_identifiers(s, op.case_id)  # the case's own ids stay readable
            params = op.payload["parameters"]
            marker = f"opsbridge-op:{op.operation_key}"
            body = f"{redact(params.get('body') or '', keep).text}\n\nOpsBridge operation marker: {marker}"
            req = TaskRequest(
                redact(params["title"], keep).text, body, params.get("assignee_group"), marker
            )
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
        cc = guard.authorize(ctx, "get_follow_up_task", "proposal.decide")

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
        cc = guard.authorize(ctx, "reconcile_operation", "proposal.decide")

        def go() -> dict[str, Any]:
            op, conn = load(cc, operation_id, need_executing=False)
            try:
                found = conn.find_by_marker(f"opsbridge-op:{op.operation_key}", timeout_seconds)
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
