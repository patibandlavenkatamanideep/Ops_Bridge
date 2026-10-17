"""MCP client used by the worker: service token + delegation over streamable HTTP."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from opsbridge.connectors.tasks import (
    AmbiguousFailure,
    ConnectorCapabilities,
    DefiniteFailure,
    LookupUnavailable,
    RemoteTask,
    TaskRequest,
)

from .auth import DELEGATION_HEADER, DelegationSigner


class McpDenied(Exception):
    """The MCP service refused the call (authorization or validation)."""


class McpTransportError(Exception):
    """The call may or may not have reached the tool (timeout, connection loss)."""


def describe_error(exc: BaseException) -> str:
    """The SDK's task group wraps the real transport errors in exception groups; name the
    leaf errors so an attempt record says what failed (e.g. a read timeout)."""
    if not isinstance(exc, BaseExceptionGroup):
        return f"{type(exc).__name__}: {exc}"
    leaves: list[str] = []
    pending: list[BaseException] = list(exc.exceptions)
    while pending:
        e = pending.pop(0)
        if isinstance(e, BaseExceptionGroup):
            pending[:0] = list(e.exceptions)
        else:
            leaves.append(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
    return f"{type(exc).__name__} of {len(leaves)}: " + "; ".join(leaves)


@dataclass
class ClientCredentialsTokens:
    """OAuth client-credentials token provider with caching (worker service identity)."""

    token_url: str
    client_id: str
    client_secret: str
    _token: str | None = field(default=None, init=False, repr=False)
    _expires: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __call__(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 30:
                return self._token
            r = httpx.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=10,
            )
            r.raise_for_status()
            body = r.json()
            self._token = str(body["access_token"])
            self._expires = time.time() + int(body.get("expires_in", 60))
            return self._token


@dataclass
class McpToolClient:
    url: str
    service_token: Callable[[], str]
    timeout_seconds: float = 10.0

    def call(self, tool: str, args: dict[str, Any], delegation: str) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {self.service_token()}",
                DELEGATION_HEADER: delegation,
            }
            http = httpx2.AsyncClient(headers=headers, timeout=self.timeout_seconds)
            async with Client(
                streamable_http_client(self.url, http_client=http),
                read_timeout_seconds=self.timeout_seconds,
            ) as c:
                result = await c.call_tool(tool, args)
            text = "".join(getattr(b, "text", "") for b in result.content)
            if result.is_error:
                raise McpDenied(text[:500] or "tool error")
            if result.structured_content:
                return dict(result.structured_content)
            return dict(json.loads(text)) if text else {}

        try:
            return anyio.run(go)
        except McpDenied:
            raise
        except (
            Exception
        ) as exc:  # transport errors surface as exception groups from the SDK task group
            raise McpTransportError(describe_error(exc)[:300]) from exc

    def list_tools(self, delegation: str | None = None) -> list[str]:
        async def go() -> list[str]:
            headers = {"Authorization": f"Bearer {self.service_token()}"}
            if delegation:
                headers[DELEGATION_HEADER] = delegation
            http = httpx2.AsyncClient(headers=headers, timeout=self.timeout_seconds)
            async with Client(streamable_http_client(self.url, http_client=http)) as c:
                return [t.name for t in (await c.list_tools()).tools]

        return anyio.run(go)


@dataclass
class McpCaseworkConnector:
    """TaskConnector that executes through Casework MCP for one operation."""

    client: McpToolClient
    signer: DelegationSigner
    tenant_id: uuid.UUID
    operation_id: uuid.UUID
    approver_id: uuid.UUID
    case_id: uuid.UUID
    capabilities: ConnectorCapabilities
    name: str = "casework_mcp"
    version: str = "casework-mcp/1"
    audience: str = "opsbridge-casework-mcp"

    def _delegation(self) -> str:
        return self.signer.sign(
            tenant_id=self.tenant_id,
            principal_id=self.approver_id,
            audience=self.audience,
            tools=["create_follow_up_task", "get_follow_up_task", "reconcile_operation"],
            case_id=self.case_id,
            operation_id=self.operation_id,
            ttl_seconds=120,
        )

    def create(self, req: TaskRequest, idempotency_key: str | None, timeout: float) -> RemoteTask:
        try:
            out = self.client.call(
                "create_follow_up_task",
                {"operation_id": str(self.operation_id)},
                self._delegation(),
            )
        except McpDenied as exc:
            raise DefiniteFailure(f"casework service refused: {exc}", retryable=False) from exc
        except McpTransportError as exc:
            raise AmbiguousFailure(f"MCP call outcome unknown: {exc}") from exc
        if out.get("outcome") == "created":
            return RemoteTask(
                str(out["remote_ref"]),
                out.get("url"),
                out.get("marker"),
                str(out.get("title", "")),
                out,
            )
        if out.get("outcome") == "definite_failure":
            raise DefiniteFailure(str(out.get("detail")), retryable=bool(out.get("retryable")))
        raise AmbiguousFailure(str(out.get("detail", "ambiguous result from casework service")))

    def get(self, remote_ref: str, timeout: float) -> RemoteTask | None:
        try:
            out = self.client.call(
                "get_follow_up_task",
                {"operation_id": str(self.operation_id), "remote_ref": remote_ref},
                self._delegation(),
            )
        except (McpDenied, McpTransportError) as exc:
            raise LookupUnavailable(f"read-back via MCP failed: {exc}") from exc
        if out.get("outcome") == "not_found":
            return None
        if out.get("outcome") != "found":
            raise LookupUnavailable(str(out.get("detail", "read-back unavailable")))
        return RemoteTask(
            str(out["remote_ref"]),
            out.get("url"),
            out.get("marker"),
            str(out.get("title", "")),
            out,
        )

    def find_by_marker(self, marker: str, timeout: float) -> list[RemoteTask]:
        try:
            out = self.client.call(
                "reconcile_operation", {"operation_id": str(self.operation_id)}, self._delegation()
            )
        except (McpDenied, McpTransportError) as exc:
            raise LookupUnavailable(f"reconciliation via MCP failed: {exc}") from exc
        if out.get("outcome") != "searched":
            raise LookupUnavailable(str(out.get("detail", "lookup unavailable")))
        return [
            RemoteTask(
                str(m["remote_ref"]), m.get("url"), m.get("marker"), str(m.get("title", "")), m
            )
            for m in out.get("matches", [])
        ]
