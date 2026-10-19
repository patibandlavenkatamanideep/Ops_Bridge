"""Authenticated case API: isolation, CSRF, idempotency, sessions (OB-09, OB-10, OB-16)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from opsbridge.api.app import create_app
from opsbridge.auth.sessions import COOKIE_NAME, create_session, hash_token
from opsbridge.config import Settings, get_settings
from opsbridge.persistence.db import Database, DbContext, make_engine

from .conftest import add_member, make_tenant, make_user

pytestmark = pytest.mark.integration


@dataclass
class Actor:
    user_id: uuid.UUID
    token: str
    csrf: str


@pytest.fixture
def api_settings() -> Settings:
    return get_settings().model_copy(update={"rate_limit_per_minute": 1000})


@pytest.fixture
def client(app_db: Database, api_settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings=api_settings, db=app_db)
    with TestClient(app, base_url="http://localhost:5173") as c:
        yield c


def login(db: Database, settings: Settings, user_id: uuid.UUID) -> Actor:
    token = create_session(db, settings, user_id, id_token=None)
    with db.session(DbContext(session_hash=hash_token(token))) as s:
        csrf = s.execute(text("SELECT csrf_token FROM user_sessions")).scalar_one()
    return Actor(user_id, token, csrf)


def call(client: TestClient, actor: Actor | None, method: str, url: str, **kw):  # type: ignore[no-untyped-def]
    headers = kw.pop("headers", {})
    cookies = {COOKIE_NAME: actor.token} if actor else {}
    if actor and method in ("POST", "PUT", "PATCH", "DELETE") and "X-CSRF-Token" not in headers:
        headers["X-CSRF-Token"] = actor.csrf
    client.cookies.clear()
    for k, v in cookies.items():
        client.cookies.set(k, v)
    return client.request(method, url, headers=headers, **kw)


@pytest.fixture
def world(migrator_engine: Engine, app_db: Database, api_settings: Settings):  # type: ignore[no-untyped-def]
    furn = make_tenant(migrator_engine, "demo-furniture")
    it = make_tenant(migrator_engine, "demo-it", pack="it_hardware", tier="enhanced")
    users = {
        name: make_user(migrator_engine, name)
        for name in ("req1", "req2", "inv", "admin", "it_req")
    }
    add_member(migrator_engine, furn, users["req1"], ["requester"])
    add_member(migrator_engine, furn, users["req2"], ["requester"])
    add_member(migrator_engine, furn, users["inv"], ["investigator"])
    add_member(migrator_engine, furn, users["admin"], ["tenant_admin"])
    add_member(migrator_engine, it, users["it_req"], ["requester"])
    actors = {name: login(app_db, api_settings, uid) for name, uid in users.items()}
    return {"furn": furn, "it": it, **actors}


NEW_CASE = {
    "title": "Invoice INV-9001 blocked",
    "invoice_number": "INV-9001",
    "po_number": "PO-1001",
}


def test_unauthenticated_requests_get_consistent_error_body(client: TestClient) -> None:
    r = client.get("/api/v1/tenants/demo-furniture/cases")
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "unauthenticated"
    assert body["correlation_id"] == r.headers["X-Correlation-Id"]
    assert body["retryable"] is False


def test_requester_creates_and_reads_own_case(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    r = call(client, world["req1"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE)
    assert r.status_code == 201, r.text
    case = r.json()
    assert case["case_number"] == "CASE-00001"
    assert case["status"] == "new"
    r = call(client, world["req1"], "GET", f"/api/v1/tenants/demo-furniture/cases/{case['id']}")
    assert r.status_code == 200
    queue = call(client, world["req1"], "GET", "/api/v1/tenants/demo-furniture/cases").json()
    assert [i["id"] for i in queue["items"]] == [case["id"]]


def test_requester_cannot_see_other_requesters_case_but_investigator_can(
    client: TestClient, world
) -> None:  # type: ignore[no-untyped-def]
    case = call(
        client, world["req1"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE
    ).json()
    r = call(client, world["req2"], "GET", f"/api/v1/tenants/demo-furniture/cases/{case['id']}")
    assert r.status_code == 404
    assert (
        call(client, world["req2"], "GET", "/api/v1/tenants/demo-furniture/cases").json()["items"]
        == []
    )
    r = call(client, world["inv"], "GET", f"/api/v1/tenants/demo-furniture/cases/{case['id']}")
    assert r.status_code == 200


def test_other_tenant_cannot_access_case_by_any_route(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    case = call(
        client, world["req1"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE
    ).json()
    # Asking for the furniture tenant scope without membership: tenant "not found".
    r = call(client, world["it_req"], "GET", f"/api/v1/tenants/demo-furniture/cases/{case['id']}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    # Using their own tenant scope with the foreign case id: case "not found", no leak.
    r = call(client, world["it_req"], "GET", f"/api/v1/tenants/demo-it/cases/{case['id']}")
    assert r.status_code == 404
    assert "INV-9001" not in r.text
    # Creating in a tenant they don't belong to is also refused.
    r = call(client, world["it_req"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE)
    assert r.status_code == 404


def test_csrf_token_required_for_writes(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    r = call(
        client,
        world["req1"],
        "POST",
        "/api/v1/tenants/demo-furniture/cases",
        json=NEW_CASE,
        headers={"X-CSRF-Token": "wrong"},
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_rejected"
    r = call(
        client,
        world["req1"],
        "POST",
        "/api/v1/tenants/demo-furniture/cases",
        json=NEW_CASE,
        headers={"X-CSRF-Token": world["req1"].csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_idempotent_double_submit_and_duplicate_invoice(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    body = {"title": "Manual case without invoice number"}
    h = {"Idempotency-Key": "abc-123"}
    first = call(
        client,
        world["req1"],
        "POST",
        "/api/v1/tenants/demo-furniture/cases",
        json=body,
        headers=dict(h),
    )
    second = call(
        client,
        world["req1"],
        "POST",
        "/api/v1/tenants/demo-furniture/cases",
        json=body,
        headers=dict(h),
    )
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    call(client, world["req1"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE)
    dup = call(client, world["req2"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE)
    assert dup.status_code == 409
    assert dup.json()["error"]["details"] == {}  # req2 may not learn the other case id


def test_role_without_case_permission_is_forbidden(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    r = call(client, world["admin"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE)
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required_permission"] == "case.create"


def test_queue_pagination(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    for i in range(5):
        call(
            client,
            world["inv"],
            "POST",
            "/api/v1/tenants/demo-furniture/cases",
            json={"title": f"case {i}", "invoice_number": f"INV-{i}"},
        )
    page1 = call(client, world["inv"], "GET", "/api/v1/tenants/demo-furniture/cases?limit=2").json()
    page2 = call(
        client,
        world["inv"],
        "GET",
        f"/api/v1/tenants/demo-furniture/cases?limit=2&cursor={page1['next_cursor']}",
    ).json()
    page3 = call(
        client,
        world["inv"],
        "GET",
        f"/api/v1/tenants/demo-furniture/cases?limit=2&cursor={page2['next_cursor']}",
    ).json()
    ids = [i["id"] for p in (page1, page2, page3) for i in p["items"]]
    assert len(ids) == 5 and len(set(ids)) == 5
    assert page3["next_cursor"] is None
    bad = call(
        client, world["inv"], "GET", "/api/v1/tenants/demo-furniture/cases?cursor=notacursor"
    )
    assert bad.status_code == 422


def test_expired_and_revoked_sessions_are_rejected(
    client: TestClient, world, app_db: Database
) -> None:  # type: ignore[no-untyped-def]
    actor = world["req1"]
    assert call(client, actor, "GET", "/api/v1/me").status_code == 200
    with app_db.session(DbContext(session_hash=hash_token(actor.token))) as s:
        s.execute(
            text("UPDATE user_sessions SET idle_expires_at = :t"),
            {"t": datetime.now(UTC) - timedelta(seconds=1)},
        )
    assert call(client, actor, "GET", "/api/v1/me").status_code == 401
    other = world["req2"]
    r = call(client, other, "POST", "/auth/logout")
    assert r.status_code == 200
    assert call(client, other, "GET", "/api/v1/me").status_code == 401


def test_me_lists_server_side_memberships(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    me = call(client, world["inv"], "GET", "/api/v1/me").json()
    assert [m["tenant_slug"] for m in me["memberships"]] == ["demo-furniture"]
    assert "proposal.decide" not in me["memberships"][0]["permissions"]


def test_case_survives_application_restart(world, api_settings: Settings) -> None:  # type: ignore[no-untyped-def]
    db1 = Database(make_engine("app", database="opsbridge_test"))
    with TestClient(
        create_app(settings=api_settings, db=db1), base_url="http://localhost:5173"
    ) as c1:
        case = call(
            c1, world["req1"], "POST", "/api/v1/tenants/demo-furniture/cases", json=NEW_CASE
        ).json()
    db1.engine.dispose()  # simulate process exit: all pooled connections closed
    db2 = Database(make_engine("app", database="opsbridge_test"))
    with TestClient(
        create_app(settings=api_settings, db=db2), base_url="http://localhost:5173"
    ) as c2:
        r = call(c2, world["req1"], "GET", f"/api/v1/tenants/demo-furniture/cases/{case['id']}")
        assert r.status_code == 200 and r.json()["case_number"] == case["case_number"]
    db2.engine.dispose()


def test_rate_limit_and_body_limit(app_db: Database, world) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings().model_copy(
        update={"rate_limit_per_minute": 3, "max_request_bytes": 200}
    )
    with TestClient(
        create_app(settings=settings, db=app_db), base_url="http://localhost:5173"
    ) as c:
        codes = [call(c, world["req1"], "GET", "/api/v1/me").status_code for _ in range(4)]
        assert codes[:3] == [200, 200, 200] and codes[3] == 429
        big = call(
            c,
            world["req2"],
            "POST",
            "/api/v1/tenants/demo-furniture/cases",
            content=b"x" * 500,
            headers={"Content-Type": "application/json"},
        )
        assert big.status_code == 413


def test_cors_allows_only_configured_origin(client: TestClient) -> None:
    ok = client.options(
        "/api/v1/me",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.options(
        "/api/v1/me",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in bad.headers


def test_queue_search_matches_identifiers_and_escapes_wildcards(client: TestClient, world) -> None:  # type: ignore[no-untyped-def]
    for i, inv in enumerate(("INV-100", "INV-200", "INV-300")):
        call(
            client,
            world["inv"],
            "POST",
            "/api/v1/tenants/demo-furniture/cases",
            json={"title": f"Blocked invoice {i}", "invoice_number": inv, "po_number": f"PO-7{i}"},
        )
    found = call(
        client, world["inv"], "GET", "/api/v1/tenants/demo-furniture/cases?q=inv-200"
    ).json()["items"]
    assert [c["title"] for c in found] == ["Blocked invoice 1"]
    assert (
        len(
            call(
                client, world["inv"], "GET", "/api/v1/tenants/demo-furniture/cases?q=PO-72"
            ).json()["items"]
        )
        == 1
    )
    assert (
        call(client, world["inv"], "GET", "/api/v1/tenants/demo-furniture/cases?q=%25").json()[
            "items"
        ]
        == []
    )
    assert (
        call(client, world["inv"], "GET", "/api/v1/tenants/demo-furniture/cases?q=_").json()[
            "items"
        ]
        == []
    )
