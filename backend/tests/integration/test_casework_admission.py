"""One admission contract for the executor and the Casework MCP tool (production review A05).

Real MCP client and server, PostgreSQL, Keycloak service tokens and the simulator.

Before: the casework tool ran weaker checks than the executor (no expiry, cancellation or
evidence check, though its docstring promised an unexpired approval), so a create delayed
past such a change still landed; it rebuilt the outgoing text from the raw parameters with
its own redactor instead of sending the approved text; a replayed request with a valid
delegation created another task outside the executor's attempt ledger; and recovering a
write that had landed needed the approver's current authority, so revoking the approver
blocked reconciliation. Now the tool admits a create with the executor's own admission
function at the moment it would send, once per attempt the executor opened, sends the
sealed text, and reads back or reconciles an admitted write under a separate recovery rule.
"""

# ruff: noqa: F811 - the `mcp` fixture is imported from test_mcp and named as a parameter

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from opsbridge.connectors.tasks import RemoteTask, SimTaskConnector, TaskRequest
from opsbridge.mcp_services import casework
from opsbridge.mcp_services.client import McpCaseworkConnector, McpDenied
from opsbridge.persistence.db import Database, DbContext

from .stack import MASTER
from .test_approvals_execution import approve, drain, operations, prepared, remote_tasks
from .test_evidence_freshness import correct
from .test_mcp import CASEWORK_AUD, _deleg, _tenant, mcp  # noqa: F401
from .test_member_management import member_ids, patch

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]
TOOLS = ["create_follow_up_task", "get_follow_up_task", "reconcile_operation"]


@dataclass
class _Hop:
    """The worker's casework connector, with something happening while the call travels."""

    inner: McpCaseworkConnector
    before: Any = None  # runs after the executor admitted the write, before the tool sees it
    after: Any = None  # runs after the tool answered (a retransmitted request, say)
    seen: list[Any] = field(default_factory=list)

    @property
    def capabilities(self) -> Any:
        return self.inner.capabilities

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def version(self) -> str:
        return self.inner.version

    def create(self, req: TaskRequest, key: str | None, timeout: float) -> RemoteTask:
        if self.before:
            self.before()
        task = self.inner.create(req, key, timeout)
        if self.after:
            self.seen.append(self.after(req))
        return task

    def get(self, remote_ref: str, timeout: float) -> RemoteTask | None:
        return self.inner.get(remote_ref, timeout)

    def find_by_marker(self, marker: str, timeout: float) -> list[RemoteTask]:
        return self.inner.find_by_marker(marker, timeout)


def _route(st: dict[str, Any], m: dict[str, Any], hop: dict[str, Any]) -> None:
    """Send the executor's writes through the casework tool, wrapped in a _Hop."""
    from dataclasses import replace

    def factory(tid: uuid.UUID, name: str, op: dict[str, Any]) -> _Hop:
        caps = SimTaskConnector(st["base"], MASTER, "demo-furniture").capabilities
        inner = McpCaseworkConnector(
            client=m["casework"],
            signer=m["signer"],
            tenant_id=tid,
            operation_id=op["operation_id"],
            approver_id=op["approved_by"],
            case_id=op["case_id"],
            capabilities=caps,
        )
        hop["hop"] = _Hop(inner, hop.get("before"), hop.get("after"))
        return hop["hop"]

    st["exec"]["exec"] = replace(st["exec"]["exec"], connector_for=factory, timeout_seconds=3.0)


def _sql(engine: Engine, tenant: uuid.UUID, statement: str, **params: Any) -> None:
    with Database(engine).session(DbContext(tenant_id=tenant)) as s:
        s.execute(text(statement), params)


@pytest.mark.parametrize("change", ["expired", "cancelled", "corrected"])
def test_a_create_delayed_past_a_change_is_not_admitted_by_the_tool(
    stack, mcp, migrator_engine: Engine, change: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    original = st["exec"]["exec"]
    case_id, prop = prepared(st, "INV-9001")
    op = approve(st, case_id, prop).json()["operation"]
    tid = _tenant(st)

    def meanwhile() -> None:
        if change == "expired":
            _sql(
                migrator_engine,
                tid,
                "UPDATE approval_decisions SET expires_at = now() - interval '1 second' "
                "WHERE id = (SELECT approval_id FROM operations WHERE id = :op)",
                op=op["id"],
            )
        elif change == "cancelled":
            _sql(
                migrator_engine,
                tid,
                "UPDATE operations SET cancellation = 'requested' WHERE id = :op",
                op=op["id"],
            )
        else:
            correct(st, case_id)  # the evidence the approval relied on changed

    _route(st, mcp, {"before": meanwhile})
    try:
        drain(st)
    finally:
        st["exec"]["exec"] = original
    [done] = operations(st, case_id)
    assert remote_tasks(st, op) == []  # nothing was sent
    assert done["status"] == "cancelled_before_dispatch", done


def test_a_replayed_create_cannot_make_a_second_task(stack, mcp) -> None:  # type: ignore[no-untyped-def]
    st = stack
    original = st["exec"]["exec"]
    st["store"].set_capabilities("demo-furniture", False, True)  # no idempotency keys
    case_id, prop = prepared(st, "INV-9001")
    op = approve(st, case_id, prop).json()["operation"]
    tid = _tenant(st)

    def replay(req: TaskRequest) -> list[str]:
        """The same request arriving again while the operation is still executing."""
        refused = []
        attempt = getattr(req, "attempt_id", None)  # absent before attempts were bound
        stale = _deleg(
            mcp,
            tid,
            "furn-approver",
            uuid.UUID(case_id),
            TOOLS,
            aud=CASEWORK_AUD,
            operation_id=uuid.UUID(op["id"]),
        )
        for args in (
            {"operation_id": op["id"], "deadline": req.deadline},
            {"operation_id": op["id"], "deadline": req.deadline, "attempt_id": attempt},
        ):
            try:
                mcp["casework"].call("create_follow_up_task", args, stale)
            except McpDenied as exc:
                refused.append(str(exc))
        if attempt is None:
            return refused
        bound = mcp["signer"].sign(
            tenant_id=tid,
            principal_id=mcp["users"]["furn-approver"],
            audience=CASEWORK_AUD,
            tools=TOOLS,
            case_id=uuid.UUID(case_id),
            operation_id=uuid.UUID(op["id"]),
            attempt_id=uuid.UUID(attempt),
        )
        try:
            mcp["casework"].call(
                "create_follow_up_task",
                {"operation_id": op["id"], "deadline": req.deadline, "attempt_id": attempt},
                bound,
            )
        except McpDenied as exc:
            refused.append(str(exc))
        return refused

    hop: dict[str, Any] = {"after": replay}
    _route(st, mcp, hop)
    try:
        drain(st)
    finally:
        st["exec"]["exec"] = original
        st["store"].set_capabilities("demo-furniture", True, True)
    [done] = operations(st, case_id)
    assert done["status"] == "confirmed"
    assert len(remote_tasks(st, op)) == 1, remote_tasks(st, op)
    [refused] = hop["hop"].seen
    assert len(refused) == 3, refused
    assert "already" in refused[2]  # the attempt was admitted once


def test_the_tool_sends_the_approved_text_not_a_rebuilt_one(
    stack, mcp, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    # A casework service whose redactor differs from the one the approval was sealed with.
    monkeypatch.setattr(
        casework,
        "redact",
        lambda value, keep=None: SimpleNamespace(text=value.upper()),
        raising=False,
    )
    st = stack
    original = st["exec"]["exec"]
    case_id, prop = prepared(st, "INV-9001")
    op = approve(st, case_id, prop).json()["operation"]
    _route(st, mcp, {})
    try:
        drain(st)
    finally:
        st["exec"]["exec"] = original
    [task] = remote_tasks(st, op)
    outbound = prop["outbound"]
    assert task["title"] == outbound["title"]
    assert task["body"].startswith(outbound["body"] + "\n\nOpsBridge operation marker: ")


def test_a_landed_write_is_recovered_after_the_approver_is_revoked(stack, mcp) -> None:  # type: ignore[no-untyped-def]
    st = stack
    original = st["exec"]["exec"]
    case_id, prop = prepared(st, "INV-9001")
    op = approve(st, case_id, prop).json()["operation"]
    _route(st, mcp, {})
    try:
        drain(st)
    finally:
        st["exec"]["exec"] = original
    [task] = remote_tasks(st, op)
    approver = member_ids(st)["furn-approver"]
    r = patch(st, "furn-admin", approver, status="revoked", reason="Left the company")
    assert r.status_code == 200, r.text
    d = _deleg(
        mcp,
        _tenant(st),
        "furn-approver",
        uuid.UUID(case_id),
        TOOLS,
        aud=CASEWORK_AUD,
        operation_id=uuid.UUID(op["id"]),
    )
    # Recovery reads what was already written under the authority it was written with.
    found = mcp["casework"].call("reconcile_operation", {"operation_id": op["id"]}, d)
    assert [m["remote_ref"] for m in found["matches"]] == [task["id"]]
    back = mcp["casework"].call(
        "get_follow_up_task", {"operation_id": op["id"], "remote_ref": task["id"]}, d
    )
    assert back["outcome"] == "found"
    # A new write is still refused.
    with pytest.raises(McpDenied):
        mcp["casework"].call(
            "create_follow_up_task", {"operation_id": op["id"], "attempt_id": str(uuid.uuid4())}, d
        )


@pytest.mark.parametrize("transport", ["direct", "mcp"])
@pytest.mark.parametrize("change", ["expired", "corrected"])
def test_direct_and_mcp_refuse_the_same_stale_approval(
    stack, mcp, migrator_engine: Engine, transport: str, change: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    original = st["exec"]["exec"]
    case_id, prop = prepared(st, "INV-9001")
    op = approve(st, case_id, prop).json()["operation"]
    if change == "expired":
        _sql(
            migrator_engine,
            _tenant(st),
            "UPDATE approval_decisions SET expires_at = now() - interval '1 second' "
            "WHERE id = (SELECT approval_id FROM operations WHERE id = :op)",
            op=op["id"],
        )
    else:
        correct(st, case_id)
    if transport == "mcp":
        _route(st, mcp, {})
    try:
        drain(st)
    finally:
        st["exec"]["exec"] = original
    [done] = operations(st, case_id)
    assert remote_tasks(st, op) == []
    assert (done["status"], done["failure_category"]) == (
        "cancelled_before_dispatch",
        "precondition_failed",
    )
