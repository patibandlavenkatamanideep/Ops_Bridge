"""Genuine MCP client/server exchanges over streamable HTTP (OB-08).

Real pieces: the official MCP SDK client and server, a Keycloak client-credentials token
for the worker identity, Ed25519 delegation tokens, PostgreSQL under the `opsbridge_mcp`
role, and the purchasing/task simulator behind the casework service.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text

from opsbridge.connectors.tasks import SimTaskConnector
from opsbridge.mcp_services.auth import DelegationSigner, DelegationVerifier, JwksTokenVerifier
from opsbridge.mcp_services.casework import build_casework_server
from opsbridge.mcp_services.client import (
    ClientCredentialsTokens,
    McpCaseworkConnector,
    McpDenied,
    McpToolClient,
)
from opsbridge.mcp_services.records import build_records_server
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import AuditEvent

from .servers import serve
from .stack import MASTER, get, open_case
from .test_api_cases import call
from .test_approvals_execution import approve, drain, fault, operations, prepared, remote_tasks

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]
T = "/api/v1/tenants/demo-furniture"
RECORDS_AUD, CASEWORK_AUD = "opsbridge-records-mcp", "opsbridge-casework-mcp"


@pytest.fixture
def mcp(stack, mcp_db: Database) -> Iterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    st = stack
    s = st["settings"]
    if not s.worker_client_secret.get_secret_value():
        pytest.skip("worker client secret not configured")
    key = Ed25519PrivateKey.generate()
    signer = DelegationSigner(key)
    tokens = ClientCredentialsTokens(
        f"{s.oidc_issuer}/protocol/openid-connect/token",
        "opsbridge-worker",
        s.worker_client_secret.get_secret_value(),
    )

    def verifier(
        aud: str, clients: frozenset[str] = frozenset({"opsbridge-worker"})
    ) -> JwksTokenVerifier:
        return JwksTokenVerifier(issuer=s.oidc_issuer, audience=aud, allowed_clients=clients)

    def sim_for(tid: uuid.UUID, name: str) -> SimTaskConnector:
        return SimTaskConnector(st["base"], MASTER, "demo-furniture")

    records = build_records_server(
        mcp_db,
        verifier(RECORDS_AUD),
        DelegationVerifier(key.public_key(), RECORDS_AUD),
        "http://127.0.0.1/mcp",
        s.oidc_issuer,
    )
    casework = build_casework_server(
        mcp_db,
        verifier(CASEWORK_AUD),
        DelegationVerifier(key.public_key(), CASEWORK_AUD),
        "http://127.0.0.1/mcp",
        s.oidc_issuer,
        sim_for,
        timeout_seconds=1.0,
    )
    with (
        serve(records.streamable_http_app(stateless_http=True, json_response=True)) as rbase,
        serve(casework.streamable_http_app(stateless_http=True, json_response=True)) as cbase,
    ):
        users = _users(mcp_db, st)
        yield {
            "records": McpToolClient(f"{rbase}/mcp", tokens),
            "casework": McpToolClient(f"{cbase}/mcp", tokens),
            "rbase": rbase,
            "cbase": cbase,
            "signer": signer,
            "tokens": tokens,
            "users": users,
            "verifier": verifier,
            "key": key,
        }


def _users(db: Database, st: dict[str, Any]) -> dict[str, uuid.UUID]:
    return {name: a.user_id for name, a in st["actors"].items()}


def _tenant_and_case(st: dict[str, Any], invoice: str) -> tuple[uuid.UUID, uuid.UUID]:
    case_id = open_case(st, "furn-investigator", "demo-furniture", invoice)
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (200, 202)
    drain(st)
    with st["worker"].db.session(DbContext()) as s:
        tid = s.execute(text("SELECT tenant_id FROM jobs LIMIT 1")).scalar_one()
    return tid, uuid.UUID(case_id)


def _deleg(
    m: dict[str, Any],
    tid: uuid.UUID,
    principal: str,
    case_id: uuid.UUID | None,
    tools: list[str],
    aud: str = RECORDS_AUD,
    **kw: Any,
) -> str:
    return str(
        m["signer"].sign(
            tenant_id=tid,
            principal_id=m["users"][principal],
            audience=aud,
            tools=tools,
            case_id=case_id,
            **kw,
        )
    )


# --- discovery and protocol auth ------------------------------------------------------------


def test_discovery_requires_a_valid_service_token(mcp) -> None:  # type: ignore[no-untyped-def]
    assert sorted(mcp["records"].list_tools()) == sorted(
        [
            "get_purchase_order",
            "get_invoice",
            "get_receipt_lines",
            "search_policy",
            "get_evidence_excerpt",
            "search_case_evidence",
        ]
    )
    assert sorted(mcp["casework"].list_tools()) == [
        "create_follow_up_task",
        "get_follow_up_task",
        "reconcile_operation",
    ]
    raw = httpx.post(
        f"{mcp['rbase']}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert raw.status_code == 401
    assert "resource_metadata" in raw.headers.get("www-authenticate", "")
    forged = httpx.post(
        f"{mcp['rbase']}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.e30.c2ln"},
    )
    assert forged.status_code == 401


def test_token_for_another_audience_or_unlisted_client_is_rejected(
    stack, mcp, mcp_db: Database
) -> None:  # type: ignore[no-untyped-def]
    other = build_records_server(
        mcp_db,
        mcp["verifier"]("opsbridge-other-resource"),
        DelegationVerifier(mcp["key"].public_key(), RECORDS_AUD),
        "http://127.0.0.1/mcp",
        stack["settings"].oidc_issuer,
    )
    unlisted = build_records_server(
        mcp_db,
        mcp["verifier"](RECORDS_AUD, frozenset({"some-other-client"})),
        DelegationVerifier(mcp["key"].public_key(), RECORDS_AUD),
        "http://127.0.0.1/mcp",
        stack["settings"].oidc_issuer,
    )
    for app in (other, unlisted):
        with serve(app.streamable_http_app(stateless_http=True, json_response=True)) as base:
            r = httpx.post(
                f"{base}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp['tokens']()}"},
            )
            assert r.status_code == 401


# --- records: permitted and denied calls --------------------------------------------------------


def test_permitted_records_calls_return_case_scoped_evidence(stack, mcp) -> None:  # type: ignore[no-untyped-def]
    tid, case_id = _tenant_and_case(stack, "INV-9001")
    d = _deleg(
        mcp,
        tid,
        "furn-investigator",
        case_id,
        ["get_purchase_order", "get_receipt_lines", "search_case_evidence", "search_policy"],
    )
    po = mcp["records"].call("get_purchase_order", {"po_number": "PO-1001"}, d)
    assert (
        po["lines"][0]["quantity"] == "20"
        and po["refs"]["purchase_order.supplier_id"]["locator"] == "json:/supplier/id"
    )
    rl = mcp["records"].call("get_receipt_lines", {"po_number": "PO-1001"}, d)
    assert [line["quantity"] for line in rl["lines"]] == ["18"]
    hits = mcp["records"].call("search_case_evidence", {"query": "chair"}, d)["hits"]
    assert hits and all("version_id" in h for h in hits)
    pol = mcp["records"].call("search_policy", {"topic": "price tolerance"}, d)
    assert pol["policy_ref"].startswith("furniture-ap@") and "price_tolerance" in pol["matches"]


@pytest.mark.parametrize(
    "case",
    [
        "tool_not_delegated",
        "principal_lacks_permission",
        "missing_delegation",
        "expired_delegation",
        "forged_delegation",
        "wrong_audience",
        "foreign_case",
    ],
)
def test_denied_records_calls(stack, mcp, case: str) -> None:  # type: ignore[no-untyped-def]
    tid, case_id = _tenant_and_case(stack, "INV-9001")
    tools = ["get_purchase_order"]
    if case == "tool_not_delegated":
        d = _deleg(mcp, tid, "furn-investigator", case_id, ["get_invoice"])
    elif case == "principal_lacks_permission":
        d = _deleg(mcp, tid, "furn-requester", case_id, tools)  # requesters cannot read evidence
    elif case == "missing_delegation":
        d = ""
    elif case == "expired_delegation":
        d = _deleg(mcp, tid, "furn-investigator", case_id, tools, ttl_seconds=1)
        time.sleep(2.1)
    elif case == "forged_delegation":
        d = DelegationSigner(Ed25519PrivateKey.generate()).sign(
            tenant_id=tid,
            principal_id=mcp["users"]["furn-investigator"],
            audience=RECORDS_AUD,
            tools=tools,
            case_id=case_id,
        )
    elif case == "wrong_audience":
        d = _deleg(mcp, tid, "furn-investigator", case_id, tools, aud=CASEWORK_AUD)
    else:  # a real case id, but not the delegated one: PO-1001 is not linked to it
        other = uuid.uuid4()
        d = _deleg(mcp, tid, "furn-investigator", other, tools)
    with pytest.raises(McpDenied, match="denied"):
        mcp["records"].call("get_purchase_order", {"po_number": "PO-1001"}, d)


def test_cross_tenant_delegation_sees_nothing(stack, mcp, migrator_engine) -> None:  # type: ignore[no-untyped-def]
    _tid, case_id = _tenant_and_case(stack, "INV-9001")
    with migrator_engine.begin() as conn:  # tenants are only visible to the bootstrap role here
        it_tenant = conn.execute(text("SELECT id FROM tenants WHERE slug = 'demo-it'")).scalar_one()
    # A genuinely signed delegation for the IT tenant pointing at a furniture case id.
    d = _deleg(mcp, it_tenant, "it-investigator", case_id, ["get_purchase_order"])
    with pytest.raises(McpDenied):
        mcp["records"].call("get_purchase_order", {"po_number": "PO-1001"}, d)


def test_denied_calls_are_audited(stack, mcp, mcp_db: Database) -> None:  # type: ignore[no-untyped-def]
    tid, case_id = _tenant_and_case(stack, "INV-9001")
    with pytest.raises(McpDenied):
        mcp["records"].call(
            "get_invoice",
            {"invoice_number": "INV-9001"},
            _deleg(mcp, tid, "furn-requester", case_id, ["get_invoice"]),
        )
    with mcp_db.session(DbContext(tenant_id=tid)) as s:
        rows = (
            s.execute(select(AuditEvent).where(AuditEvent.action == "mcp.records.get_invoice"))
            .scalars()
            .all()
        )
    assert rows and rows[-1].outcome == "denied" and rows[-1].actor_id == "service:opsbridge-worker"
    assert rows[-1].on_behalf_of == mcp["users"]["furn-requester"]


# --- casework: execution through MCP ---------------------------------------------------------------


def _via_mcp(st: dict[str, Any], m: dict[str, Any], tid_holder: dict[str, Any]) -> None:
    def factory(tid: uuid.UUID, name: str, op: dict[str, Any]) -> McpCaseworkConnector:
        caps = SimTaskConnector(st["base"], MASTER, "demo-furniture").capabilities
        return McpCaseworkConnector(
            client=m["casework"],
            signer=m["signer"],
            tenant_id=tid,
            operation_id=op["operation_id"],
            approver_id=op["approved_by"],
            case_id=op["case_id"],
            capabilities=caps,
        )

    st["exec"]["exec"] = replace(st["exec"]["exec"], connector_for=factory, timeout_seconds=3.0)


def test_approved_operation_executes_through_casework_mcp(stack, mcp, mcp_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    original = st["exec"]["exec"]
    _via_mcp(st, mcp, {})
    try:
        case_id, prop = prepared(st, "INV-9001")
        assert approve(st, case_id, prop).status_code == 200
        drain(st)
    finally:
        st["exec"]["exec"] = original
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["confirmation"]["method"] == "read_back"
    assert len(remote_tasks(st, op)) == 1
    with mcp_db.session(DbContext(tenant_id=_tenant(st))) as s:
        actions = [
            r.action
            for r in s.execute(
                select(AuditEvent)
                .where(AuditEvent.action.like("mcp.casework.%"))
                .order_by(AuditEvent.seq)
            ).scalars()
        ]
    assert actions[:2] == ["mcp.casework.create_follow_up_task", "mcp.casework.get_follow_up_task"]


def test_timeout_after_commit_behind_the_mcp_hop_is_reconciled(stack, mcp) -> None:  # type: ignore[no-untyped-def]
    st = stack
    original = st["exec"]["exec"]
    _via_mcp(st, mcp, {})
    try:
        case_id, prop = prepared(st, "INV-9001")
        approve(st, case_id, prop)
        fault(st, "timeout_after_commit")  # casework service's connector times out after the commit
        drain(st)
    finally:
        st["exec"]["exec"] = original
        httpx.delete(f"{st['base']}/_control/faults")
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["confirmation"]["method"] == "reconciled_by_marker"
    assert len(remote_tasks(st, op)) == 1


def test_casework_refuses_wrong_principal_and_non_executing_operations(stack, mcp) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    op = approve(st, case_id, prop).json()["operation"]  # pending_dispatch, not executing
    tid = _tenant(st)
    op_id = uuid.UUID(op["id"])
    tools = ["create_follow_up_task", "reconcile_operation"]
    as_investigator = _deleg(
        mcp,
        tid,
        "furn-investigator",
        uuid.UUID(case_id),
        tools,
        aud=CASEWORK_AUD,
        operation_id=op_id,
    )
    with pytest.raises(McpDenied):  # investigators lack proposal.decide
        mcp["casework"].call("create_follow_up_task", {"operation_id": op["id"]}, as_investigator)
    as_approver = _deleg(
        mcp, tid, "furn-approver", uuid.UUID(case_id), tools, aud=CASEWORK_AUD, operation_id=op_id
    )
    with pytest.raises(McpDenied, match="not executing"):
        mcp["casework"].call("create_follow_up_task", {"operation_id": op["id"]}, as_approver)
    other_op = _deleg(
        mcp,
        tid,
        "furn-approver",
        uuid.UUID(case_id),
        tools,
        aud=CASEWORK_AUD,
        operation_id=uuid.uuid4(),
    )
    with pytest.raises(McpDenied, match="does not cover"):
        mcp["casework"].call("create_follow_up_task", {"operation_id": op["id"]}, other_op)
    assert remote_tasks(st, op) == []
    drain(st)  # let the normal executor finish so the module stays tidy


def _tenant(st: dict[str, Any]) -> uuid.UUID:
    with st["worker"].db.session(DbContext()) as s:
        return s.execute(
            text("SELECT tenant_id FROM jobs WHERE kind = 'dispatch_operation' LIMIT 1")
        ).scalar_one()


def test_investigation_search_runs_over_records_mcp(stack, mcp, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    deps = st["deps"]
    deps.records_mcp, deps.signer = mcp["records"], mcp["signer"]
    try:
        case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9023")
        r = call(
            st["client"],
            st["actors"]["furn-investigator"],
            "POST",
            f"{T}/cases/{case_id}/investigations",
            json={"model_mode": "mock"},
        )
        drain(st)
    finally:
        deps.records_mcp, deps.signer = None, None
    inv = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert inv["explanation_origin"] == "model:mock"
    with worker_db.session(DbContext(tenant_id=_tenant_of_run(worker_db, r.json()["run_id"]))) as s:
        rows = s.execute(
            text("SELECT tool_name, transport, outcome FROM tool_invocations WHERE run_id = :r"),
            {"r": r.json()["run_id"]},
        ).all()
    assert [tuple(x) for x in rows] == [("search_case_evidence", "mcp", "ok")]


def _tenant_of_run(worker_db: Database, run_id: str) -> uuid.UUID:
    with worker_db.session(DbContext()) as s:
        return s.execute(
            text("SELECT tenant_id FROM jobs WHERE payload->>'run_id' = :r"), {"r": run_id}
        ).scalar_one()
