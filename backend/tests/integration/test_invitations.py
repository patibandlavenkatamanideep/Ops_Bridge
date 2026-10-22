"""Membership invitations against real PostgreSQL and the real API.

The risks: someone other than the invited person joins (forwarded link, unverified address),
a link works twice, after it expired or was withdrawn, or after its inviter lost authority;
the token is stored or audited; invitations leak across tenants or outside a tenant context;
retention keeps addresses forever, or clears one that an open invitation still needs.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from opsbridge.persistence.db import Database, DbContext
from opsbridge.services.invitations import token_hash

from .conftest import add_member, make_user
from .stack import get
from .test_api_cases import Actor, call, login
from .test_approvals_execution import drain
from .test_member_management import audit_rows, member, member_ids, patch, tenant_id

pytestmark = pytest.mark.integration
ORG = "demo-furniture"
T = f"/api/v1/tenants/{ORG}"


def person(
    st: dict[str, Any], engine: Engine, app_db: Database, username: str, verified: bool = True
) -> Actor:
    """A signed-in user with no membership; `verified` is what the identity provider said."""
    uid = make_user(engine, username)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET email_verified = :v WHERE id = :u"), {"v": verified, "u": uid}
        )
    return login(app_db, st["settings"], uid)


def invite(
    st: dict[str, Any], email: str, roles: list[str], who: str | Actor = "furn-admin", **kw: Any
) -> Any:
    actor = st["actors"][who] if isinstance(who, str) else who
    body = {"email": email, "roles": roles, **kw}
    return call(st["client"], actor, "POST", f"{T}/invitations", json=body)


def accept(st: dict[str, Any], actor: Actor, token: str) -> Any:
    return call(st["client"], actor, "POST", "/api/v1/invitations/accept", json={"token": token})


def code(r: Any) -> Any:
    return r.json()["error"]["code"] if r.status_code >= 400 else r.status_code


def test_an_invited_person_joins_with_the_invited_roles(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database, worker_db: Database
) -> None:
    st = stack
    nia = person(st, migrator_engine, app_db, "nia-newhire")
    r = invite(st, "  Nia-Newhire@Example.TEST ", ["investigator"])
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    [open_] = r.json()["settings"]["invitations"]
    assert open_["email"] == "nia-newhire@example.test" and open_["expired"] is False
    assert open_["invited_by"] == "furn-admin" and open_["roles"] == ["investigator"]
    assert call(st["client"], nia, "GET", f"{T}/cases").status_code == 404  # not yet a member

    r = accept(st, nia, token)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "tenant_slug": ORG,
        "tenant_name": r.json()["tenant_name"],
        "roles": ["investigator"],
    }
    assert call(st["client"], nia, "GET", f"{T}/cases").status_code == 200
    view = get(st, "furn-admin", f"{T}/settings").json()
    assert member(view, "nia-newhire")["change_reason"] == "Joined by invitation"
    assert view["invitations"] == []
    assert code(accept(st, nia, token)) == "invitation_closed"  # single use

    # Only the token's hash is stored, and neither the token nor the address is audited.
    with app_db.session(DbContext(tenant_id=tenant_id(migrator_engine))) as s:
        stored = s.execute(text("SELECT token_hash FROM membership_invitations")).scalar_one()
    assert stored == token_hash(token) and token not in stored
    events = audit_rows(worker_db, migrator_engine, "tenant.invitation_created") + audit_rows(
        worker_db, migrator_engine, "tenant.invitation_accepted"
    )
    assert len(events) == 2
    dumped = json.dumps([e[2] for e in events])
    assert token not in dumped and "nia-newhire" not in dumped


def test_only_the_invited_verified_address_can_accept(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database
) -> None:
    st = stack
    token = invite(st, "ola-owner@example.test", ["requester"]).json()["token"]
    someone_else = person(st, migrator_engine, app_db, "eve-forwarded")
    assert code(accept(st, someone_else, token)) == "not_for_this_account"
    unverified = person(st, migrator_engine, app_db, "ola-owner", verified=False)
    assert code(accept(st, unverified, token)) == "email_not_verified"  # right address, unvouched
    assert accept(st, someone_else, "x" * 43).status_code == 404  # unknown token
    assert len(get(st, "furn-admin", f"{T}/settings").json()["invitations"]) == 1  # still open
    assert call(st["client"], unverified, "GET", f"{T}/cases").status_code == 404


def test_expired_or_withdrawn_invitations_cannot_be_accepted(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database
) -> None:
    st = stack
    pia = person(st, migrator_engine, app_db, "pia-late")
    r = invite(st, "pia-late@example.test", ["auditor"])
    token, inv_id = r.json()["token"], r.json()["invitation_id"]
    with app_db.session(DbContext(tenant_id=tenant_id(migrator_engine))) as s:
        s.execute(
            text("UPDATE membership_invitations SET expires_at = now() - interval '1 second'")
        )
    assert code(accept(st, pia, token)) == "invitation_expired"
    [shown] = get(st, "furn-admin", f"{T}/settings").json()["invitations"]
    assert shown["expired"] is True
    # An expired invitation no longer blocks a new one; withdrawing it clears the list.
    revoke = f"{T}/invitations/{inv_id}/revoke"
    assert call(st["client"], st["actors"]["furn-admin"], "POST", revoke).status_code == 200
    assert (
        code(call(st["client"], st["actors"]["furn-admin"], "POST", revoke)) == "invitation_closed"
    )

    r = invite(st, "pia-late@example.test", ["auditor"])
    token2, inv2 = r.json()["token"], r.json()["invitation_id"]
    assert (
        call(
            st["client"], st["actors"]["furn-admin"], "POST", f"{T}/invitations/{inv2}/revoke"
        ).status_code
        == 200
    )
    assert code(accept(st, pia, token2)) == "invitation_closed"
    assert call(st["client"], pia, "GET", f"{T}/cases").status_code == 404


def test_invitation_rules_and_who_may_invite(stack: dict[str, Any]) -> None:
    st = stack
    for who in ("furn-investigator", "furn-auditor", "furn-approver"):
        assert invite(st, "x@example.test", ["requester"], who=who).status_code == 403, who
    assert code(invite(st, "furn-investigator@example.test", ["auditor"])) == "already_member"
    assert invite(st, "dup@example.test", ["requester"]).status_code == 201
    assert code(invite(st, "DUP@example.test", ["auditor"])) == "already_invited"
    assert code(invite(st, "not-an-address", ["requester"])) == "invalid_email"
    assert code(invite(st, "a b@example.test", ["requester"])) == "invalid_email"
    r = invite(st, "s@example.test", ["requester"], approval_scope=["assign_case"])
    assert code(r) == "invalid_approval_scope"
    assert invite(st, "r@example.test", ["superuser"]).status_code == 422
    assert invite(st, "r@example.test", []).status_code == 422
    # Another organization: the administrator has no membership there.
    other = call(
        st["client"], st["actors"]["furn-admin"], "POST", "/api/v1/tenants/demo-it/invitations",
        json={"email": "y@example.test", "roles": ["requester"]},
    )  # fmt: skip
    assert other.status_code == 404


def test_an_invitation_lapses_when_its_inviter_loses_authority(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database
) -> None:
    st = stack
    uid = make_user(migrator_engine, "furn-admin2")
    add_member(migrator_engine, tenant_id(migrator_engine), uid, ["tenant_admin"])
    b = login(app_db, st["settings"], uid)
    token = invite(st, "quinn@example.test", ["investigator"], who=b).json()["token"]
    r = patch(
        st,
        "furn-admin",
        member_ids(st)["furn-admin2"],
        roles=["auditor"],
        reason="No longer an admin",
    )
    assert r.status_code == 200
    quinn = person(st, migrator_engine, app_db, "quinn")
    assert code(accept(st, quinn, token)) == "inviter_not_authorized"
    assert call(st["client"], quinn, "GET", f"{T}/cases").status_code == 404


def test_a_revoked_member_can_rejoin_by_invitation_with_new_roles(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database, worker_db: Database
) -> None:
    st = stack
    ids = member_ids(st)
    assert (
        patch(
            st, "furn-admin", ids["furn-requester"], status="revoked", reason="Left the company"
        ).status_code
        == 200
    )
    with migrator_engine.begin() as conn:  # as if the identity provider had verified it
        conn.execute(
            text("UPDATE users SET email_verified = true WHERE username = 'furn-requester'")
        )
    token = invite(st, "furn-requester@example.test", ["auditor"]).json()["token"]
    assert accept(st, st["actors"]["furn-requester"], token).status_code == 200
    back = member(get(st, "furn-admin", f"{T}/settings").json(), "furn-requester")
    assert (back["status"], back["roles"], back["change_reason"]) == (
        "active", ["auditor"], "Rejoined by invitation",
    )  # fmt: skip
    [(_, _, details)] = audit_rows(worker_db, migrator_engine, "tenant.invitation_accepted")
    assert details["reinstated"] is True


def test_outside_a_tenant_only_the_matching_token_finds_an_invitation(
    stack: dict[str, Any], app_db: Database
) -> None:
    st = stack
    token = invite(st, "rls@example.test", ["requester"]).json()["token"]
    user = uuid.uuid4()
    q = text("SELECT count(*) FROM membership_invitations")
    with app_db.session(DbContext(user_id=user)) as s:
        assert s.execute(q).scalar_one() == 0
    with app_db.session(DbContext(user_id=user, invite_token_hash=token_hash("wrong"))) as s:
        assert s.execute(q).scalar_one() == 0
    with app_db.session(DbContext(user_id=user, invite_token_hash=token_hash(token))) as s:
        assert s.execute(q).scalar_one() == 1
        # Read-only there: the token context cannot change or create invitations.
        assert s.execute(text("UPDATE membership_invitations SET status = 'revoked'")).rowcount == 0  # type: ignore[attr-defined]


def test_retention_clears_the_address_only_after_an_invitation_is_closed_or_expired(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database, worker_db: Database
) -> None:
    st = stack
    adm = st["actors"]["furn-admin"]
    ids: dict[str, str] = {}

    def new(name: str) -> str:
        r = invite(st, f"{name}@example.test", ["requester"])
        assert r.status_code == 201, r.text
        ids[name] = r.json()["invitation_id"]
        return str(r.json()["token"])

    joined = person(st, migrator_engine, app_db, "sam-joined")
    assert accept(st, joined, new("sam-joined")).status_code == 200
    for name in ("old-withdrawn", "new-withdrawn"):
        new(name)
        revoke = f"{T}/invitations/{ids[name]}/revoke"
        assert call(st["client"], adm, "POST", revoke).status_code == 200
    new("lapsed")
    open_token = new("still-open")
    tid = tenant_id(migrator_engine)
    with app_db.session(DbContext(tenant_id=tid)) as s:  # backdate around a 30-day period
        for name, column, days in (
            ("sam-joined", "closed_at", 31),
            ("old-withdrawn", "closed_at", 31),
            ("new-withdrawn", "closed_at", 29),
            ("lapsed", "expires_at", 31),
        ):
            s.execute(
                text(
                    f"UPDATE membership_invitations SET {column} = now() - make_interval(days => :d)"
                    " WHERE id = :i"
                ),
                {"d": days, "i": ids[name]},
            )
    shown = [i["email"] for i in get(st, "furn-admin", f"{T}/settings").json()["invitations"]]
    assert shown == ["lapsed@example.test", "still-open@example.test"]  # expired, still listed

    put = {"retention": {"invitations_days": 30}}
    assert call(st["client"], adm, "PUT", f"{T}/settings", json=put).status_code == 200
    for _ in range(2):  # a second sweep finds nothing more to clear
        assert call(st["client"], adm, "POST", f"{T}/retention/run").status_code == 202
        drain(st)

    q = text(
        "SELECT id::text, email, status, roles, email_cleared_at IS NOT NULL, deletion_reason"
        " FROM membership_invitations"
    )
    with app_db.session(DbContext(tenant_id=tid)) as s:
        rows = {r[0]: tuple(r[1:]) for r in s.execute(q).all()}
    why = "retention:invitations_days=30"
    assert rows == {
        ids["sam-joined"]: (None, "accepted", ["requester"], True, why),
        ids["old-withdrawn"]: (None, "revoked", ["requester"], True, why),
        ids["lapsed"]: (None, "pending", ["requester"], True, why),
        ids["new-withdrawn"]: ("new-withdrawn@example.test", "revoked", ["requester"], False, None),
        ids["still-open"]: ("still-open@example.test", "pending", ["requester"], False, None),
    }
    sweeps = audit_rows(worker_db, migrator_engine, "retention.sweep")
    assert sorted(d["invitation_addresses_cleared"] for _, _, d in sweeps) == [0, 3]
    shown = [i["email"] for i in get(st, "furn-admin", f"{T}/settings").json()["invitations"]]
    assert shown == ["still-open@example.test"]  # a cleared invitation leaves the list
    assert call(st["client"], joined, "GET", f"{T}/cases").status_code == 200  # still a member
    still_open = person(st, migrator_engine, app_db, "still-open")
    assert accept(st, still_open, open_token).status_code == 200

    # The worker sees invitations only in a tenant's context and may change only the address.
    count = text("SELECT count(*) FROM membership_invitations")
    with worker_db.session(DbContext()) as s:
        assert s.execute(count).scalar_one() == 0
    with pytest.raises(DBAPIError, match="permission denied"):
        with worker_db.session(DbContext(tenant_id=tid)) as s:
            s.execute(text("UPDATE membership_invitations SET status = 'accepted'"))
