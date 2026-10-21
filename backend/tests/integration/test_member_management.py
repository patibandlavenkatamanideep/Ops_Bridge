"""Tenant administrators manage members: roles, approval scope, revoke and reinstate.

Real PostgreSQL (row-level security, row locks) and the real API. The risks: a change that
does not take effect, a revoked approver whose earlier approval still runs, an administrator
granting themselves authority, a change reaching another organization, two administrators
demoting each other at once, and re-seeding that quietly undoes a revocation.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from opsbridge.api.deps import resolve_principal
from opsbridge.auth.sessions import load_session
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.seed import seed_demo
from opsbridge.services import casework_admin as svc
from opsbridge.services.investigations import Conflict

from .conftest import add_member, make_user
from .stack import get, open_case
from .test_api_cases import Actor, call, login
from .test_approvals_execution import ORG, approve, drain, prepared, remote_tasks

pytestmark = pytest.mark.integration
T = f"/api/v1/tenants/{ORG}"


# Lock waits in this test database only (pg_locks covers the whole server).
WAITING = text(
    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE NOT l.granted AND a.datname = current_database()"
)


def member_ids(st: dict[str, Any]) -> dict[str, str]:
    view = get(st, "furn-admin", f"{T}/settings").json()
    return {m["username"]: m["id"] for m in view["members"]}


def member(view: dict[str, Any], username: str) -> dict[str, Any]:
    return next(m for m in view["members"] if m["username"] == username)


def patch(st: dict[str, Any], who: str | Actor, mid: str, **body: Any) -> Any:
    actor = st["actors"][who] if isinstance(who, str) else who
    return call(st["client"], actor, "PATCH", f"{T}/members/{mid}", json=body)


def tenant_id(engine: Engine) -> uuid.UUID:
    with engine.begin() as conn:
        tid: uuid.UUID = conn.execute(
            text("SELECT id FROM tenants WHERE slug = :s"), {"s": ORG}
        ).scalar_one()
    return tid


def audit_rows(worker_db: Database, engine: Engine, action: str) -> list[Any]:
    # Audit events are tenant rows under forced RLS: read them in the tenant's context.
    with worker_db.session(DbContext(tenant_id=tenant_id(engine))) as s:
        return list(
            s.execute(
                text("SELECT actor_id, resource_id, details FROM audit_events WHERE action = :a"),
                {"a": action},
            ).all()
        )


def second_admin(st: dict[str, Any], engine: Engine, app_db: Database) -> tuple[Actor, str]:
    uid = make_user(engine, "furn-admin2")
    add_member(engine, tenant_id(engine), uid, ["tenant_admin"])
    return login(app_db, st["settings"], uid), member_ids(st)["furn-admin2"]


def test_a_role_change_applies_on_the_next_request_and_is_audited(
    stack: dict[str, Any], migrator_engine: Engine, worker_db: Database
) -> None:
    st = stack
    ids = member_ids(st)
    case_id = open_case(st, "furn-investigator", ORG, "INV-9001")
    assert get(st, "furn-investigator", f"{T}/cases/{case_id}/proposals").status_code == 200

    r = patch(
        st,
        "furn-admin",
        ids["furn-investigator"],
        roles=["requester"],
        reason="Moved to purchasing",
    )
    assert r.status_code == 200, r.text
    changed = member(r.json(), "furn-investigator")
    assert changed["roles"] == ["requester"] and changed["change_reason"] == "Moved to purchasing"
    assert changed["changed_at"] is not None
    # No sign-out needed: authority comes from the membership on every request.
    assert get(st, "furn-investigator", f"{T}/cases/{case_id}/proposals").status_code == 403

    [(actor, resource, details)] = audit_rows(worker_db, migrator_engine, "tenant.member_updated")
    assert actor == str(st["actors"]["furn-admin"].user_id) and resource == ids["furn-investigator"]
    assert details["before"]["roles"] == ["investigator"] and details["after"]["roles"] == [
        "requester"
    ]
    assert "Moved" not in json.dumps(details)  # free text stays on the membership row


def test_a_revoked_member_loses_access_and_reinstating_restores_it(stack: dict[str, Any]) -> None:
    st = stack
    ids = member_ids(st)
    r = patch(st, "furn-admin", ids["furn-requester"], status="revoked", reason="Left the company")
    assert r.status_code == 200 and member(r.json(), "furn-requester")["status"] == "revoked"
    denied = get(st, "furn-requester", f"{T}/cases")
    assert denied.status_code == 404  # existence of the organization is not confirmed either

    r = patch(st, "furn-admin", ids["furn-requester"], status="active", reason="Rehired in AP")
    assert r.status_code == 200
    assert get(st, "furn-requester", f"{T}/cases").status_code == 200


def test_revoking_an_approver_stops_their_earlier_approval_before_dispatch(
    stack: dict[str, Any], migrator_engine: Engine, worker_db: Database
) -> None:
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    ids = member_ids(st)
    r = patch(
        st, "furn-admin", ids["furn-approver"], status="revoked", reason="Approval rights withdrawn"
    )
    assert r.status_code == 200
    drain(st)
    [op] = get(st, "furn-auditor", f"{T}/cases/{case_id}/operations").json()["items"]
    assert op["status"] == "cancelled_before_dispatch"
    assert remote_tasks(st, op) == []  # nothing reached the tracker
    [(_, _, details)] = audit_rows(
        worker_db, migrator_engine, "operation.cancelled_before_dispatch"
    )
    assert "approver no longer holds approval authority" in details["detail"]


def test_member_changes_are_validated_and_limited_to_administrators(stack: dict[str, Any]) -> None:
    st = stack
    ids = member_ids(st)
    req = ids["furn-requester"]

    def code(r: Any) -> Any:
        return r.json()["error"]["code"] if r.status_code >= 400 else r.status_code

    for who in ("furn-investigator", "furn-auditor", "furn-approver"):
        r = patch(st, who, req, status="revoked", reason="not mine to decide")
        assert r.status_code == 403, who
    # No self-grants: another administrator must change an administrator's membership.
    r = patch(
        st, "furn-admin", ids["furn-admin"], roles=["tenant_admin", "approver"],
        approval_scope=["create_follow_up_task"], reason="I approve my own changes",
    )  # fmt: skip
    assert code(r) == "self_change"
    assert (
        patch(st, "furn-admin", req, roles=["superuser"], reason="unknown role").status_code == 422
    )
    r = patch(
        st,
        "furn-admin",
        ids["furn-approver"],
        approval_scope=["pay_invoice"],
        reason="unknown action",
    )
    assert r.status_code == 422
    assert patch(st, "furn-admin", req, status="revoked", reason="    ").status_code == 422
    assert code(patch(st, "furn-admin", req, roles=[], reason="no roles at all")) == "invalid_roles"
    r = patch(st, "furn-admin", req, approval_scope=["assign_case"], reason="scope without role")
    assert code(r) == "invalid_approval_scope"
    assert (
        code(patch(st, "furn-admin", req, roles=["requester"], reason="same as before"))
        == "no_change"
    )

    # Dropping the approver role clears the approval scope with it.
    r = patch(st, "furn-admin", ids["furn-approver2"], roles=["auditor"], reason="Now audits notes")
    assert r.status_code == 200
    assert member(r.json(), "furn-approver2")["approval_scope"] == []
    unchanged = member(get(st, "furn-admin", f"{T}/settings").json(), "furn-requester")
    assert unchanged["status"] == "active" and unchanged["roles"] == ["requester"]


def test_administrators_cannot_change_another_organizations_members(
    stack: dict[str, Any], migrator_engine: Engine
) -> None:
    st = stack
    with migrator_engine.begin() as conn:
        it_member = str(
            conn.execute(
                text(
                    "SELECT m.id FROM memberships m JOIN users u ON u.id = m.user_id "
                    "JOIN tenants t ON t.id = m.tenant_id "
                    "WHERE t.slug = 'demo-it' AND u.username = 'it-investigator'"
                )
            ).scalar_one()
        )
    body = {"status": "revoked", "reason": "cross-tenant probe"}
    actor = st["actors"]["furn-admin"]
    assert patch(st, "furn-admin", it_member, **body).status_code == 404
    r = call(
        st["client"], actor, "PATCH", f"/api/v1/tenants/demo-it/members/{it_member}", json=body
    )
    assert r.status_code == 404
    with migrator_engine.begin() as conn:
        status = conn.execute(
            text("SELECT status FROM memberships WHERE id = :i"), {"i": it_member}
        ).scalar_one()
    assert status == "active"


def test_an_administrator_demoted_mid_request_cannot_finish_it(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database
) -> None:
    st = stack
    b, b_id = second_admin(st, migrator_engine, app_db)
    info = load_session(app_db, st["settings"], b.token)
    assert info is not None
    stale = resolve_principal(app_db, info, ORG)  # resolved as an administrator
    assert stale is not None and stale.has("tenant.manage_members")

    r = patch(st, "furn-admin", b_id, roles=["auditor"], reason="Admin rights withdrawn")
    assert r.status_code == 200
    with pytest.raises(Conflict) as exc:
        svc.update_membership(
            app_db, stale, uuid.UUID(member_ids(st)["furn-requester"]),
            roles=None, approval_scope=None, status="revoked",
            reason="request still in flight", correlation_id=None,
        )  # fmt: skip
    assert exc.value.code == "authority_changed"
    assert (
        member(get(st, "furn-admin", f"{T}/settings").json(), "furn-requester")["status"]
        == "active"
    )


def test_two_administrators_demoting_each_other_at_once_leave_one(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database
) -> None:
    st = stack
    a = st["actors"]["furn-admin"]
    b, b_id = second_admin(st, migrator_engine, app_db)
    a_id = member_ids(st)["furn-admin"]
    app = st["client"].app
    results: dict[str, int] = {}

    def demote(name: str, actor: Actor, target: str) -> None:
        client = TestClient(app, base_url="http://localhost:5173")  # one client per thread
        r = call(client, actor, "PATCH", f"{T}/members/{target}",
                 json={"roles": ["auditor"], "reason": "concurrent demotion"})  # fmt: skip
        results[name] = r.status_code

    tid = tenant_id(migrator_engine)
    threads = [
        threading.Thread(target=demote, args=("a", a, b_id)),
        threading.Thread(target=demote, args=("b", b, a_id)),
    ]
    # Hold the tenant's membership rows so both requests are in flight before either proceeds.
    with migrator_engine.connect() as holder, migrator_engine.connect() as probe:
        tx = holder.begin()
        holder.execute(
            text("SELECT id FROM memberships WHERE tenant_id = :t FOR UPDATE"), {"t": tid}
        )
        for t in threads:
            t.start()
        deadline = time.monotonic() + 15
        while probe.execute(WAITING).scalar_one() < 2:
            assert time.monotonic() < deadline, "both requests should be waiting for the lock"
            time.sleep(0.05)
        tx.rollback()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results.values()) == [200, 409]
    with migrator_engine.begin() as conn:
        admins = conn.execute(
            text(
                "SELECT count(*) FROM memberships WHERE tenant_id = :t AND status = 'active' "
                "AND 'tenant_admin' = ANY(roles)"
            ),
            {"t": tid},
        ).scalar_one()
    assert admins == 1


def test_reseeding_keeps_changes_made_in_the_app(stack: dict[str, Any]) -> None:
    st = stack
    ids = member_ids(st)
    assert (
        patch(
            st, "furn-admin", ids["furn-requester"], status="revoked", reason="Left the company"
        ).status_code
        == 200
    )
    r = patch(st, "furn-admin", ids["furn-approver2"], approval_scope=["assign_case", "record_review_note"],
              reason="Covers case assignment too")  # fmt: skip
    assert r.status_code == 200

    mdb = Database(make_engine("migrator", database="opsbridge_test"))
    try:
        report = seed_demo(mdb, st["settings"])
    finally:
        mdb.engine.dispose()
    assert report.memberships_created == 0 and report.memberships_kept > 0
    view = get(st, "furn-admin", f"{T}/settings").json()
    assert member(view, "furn-requester")["status"] == "revoked"  # not quietly reinstated
    assert member(view, "furn-approver2")["approval_scope"] == ["assign_case", "record_review_note"]
