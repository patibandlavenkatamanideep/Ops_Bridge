"""Shared authorization guard and helpers for OpsBridge MCP services."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import permissions_for
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Membership

from .auth import DELEGATION_HEADER, Delegation, DelegationError, DelegationVerifier


class ToolDenied(ToolError):
    """Raised inside a tool when the call is not authorized.

    Subclasses the SDK's `ToolError` (an anticipated failure): the caller receives
    `is_error=True` with this safe message; other exceptions are masked by the SDK.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"denied: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class CallContext:
    service_client: str
    delegation: Delegation
    permissions: frozenset[str]

    @property
    def db_ctx(self) -> DbContext:
        return DbContext(tenant_id=self.delegation.tenant_id, user_id=self.delegation.principal_id)


@dataclass
class Guard:
    db: Database
    delegation_verifier: DelegationVerifier
    service_name: str

    def authorize(
        self,
        ctx: Context,
        tool: str,
        permission: str,
        extra: Callable[[CallContext], str | None] | None = None,
    ) -> CallContext:
        token = get_access_token()
        if token is None:  # the bearer middleware normally rejects first; defence in depth
            raise ToolDenied("no service identity")
        request = getattr(ctx.request_context, "request", None)
        raw = request.headers.get(DELEGATION_HEADER) if request is not None else None
        try:
            d = self.delegation_verifier.verify(raw)
        except DelegationError as exc:
            raise ToolDenied(str(exc)) from exc
        if tool not in d.tools:
            self._audit(d, token.client_id, tool, "denied", "tool not in delegation")
            raise ToolDenied(f"tool {tool} is not delegated for this call")
        with self.db.session(DbContext(tenant_id=d.tenant_id, user_id=d.principal_id)) as s:
            m = s.execute(
                select(Membership).where(
                    Membership.user_id == d.principal_id,
                    Membership.tenant_id == d.tenant_id,
                    Membership.status == "active",
                )
            ).scalar_one_or_none()
            perms = permissions_for(list(m.roles)) if m else frozenset()
        if permission not in perms:
            self._audit(d, token.client_id, tool, "denied", f"principal lacks {permission}")
            raise ToolDenied(f"the delegated principal lacks {permission}")
        cc = CallContext(token.client_id, d, perms)
        if extra is not None:
            problem = extra(cc)
            if problem:
                self._audit(d, token.client_id, tool, "denied", problem)
                raise ToolDenied(problem)
        return cc

    def _audit(
        self,
        d: Delegation,
        client: str,
        tool: str,
        outcome: str,
        detail: str,
        resource_id: str | None = None,
    ) -> None:
        with self.db.session(DbContext(tenant_id=d.tenant_id)) as s:
            audit.record(
                s,
                tenant_id=d.tenant_id,
                actor_kind="service",
                actor_id=f"service:{client}",
                on_behalf_of=d.principal_id,
                action=f"mcp.{self.service_name}.{tool}",
                resource_type="mcp_tool",
                resource_id=resource_id or tool,
                outcome=outcome,
                details={"detail": detail[:300], "jti": d.jti},
            )

    def audit_ok(self, cc: CallContext, tool: str, resource_id: str) -> None:
        self._audit(cc.delegation, cc.service_client, tool, "ok", "", resource_id)


def as_uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ToolDenied(f"invalid {what}") from exc


def jsonable(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def jsonable_dict(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = jsonable(value)
    return out
