"""Real OIDC Authorization Code + PKCE against the local Keycloak (OB-09).

These tests drive Keycloak's actual login form over HTTP. No mocks.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from opsbridge.api.app import create_app
from opsbridge.config import Settings, get_settings
from opsbridge.persistence.db import Database, make_engine
from opsbridge.seed import seed_demo

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]


@pytest.fixture
def settings() -> Settings:
    s = get_settings()
    if s.demo_user_password is None:
        pytest.skip("OPSBRIDGE_DEMO_USER_PASSWORD not configured (run make setup)")
    return s.model_copy(update={"rate_limit_per_minute": 1000})


@pytest.fixture
def seeded(settings: Settings) -> None:
    db = Database(make_engine("migrator", database="opsbridge_test"))
    seed_demo(db, settings)
    db.engine.dispose()


@pytest.fixture
def client(app_db: Database, settings: Settings, seeded: None) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings=settings, db=app_db),
        base_url="http://localhost:5173",
        follow_redirects=False,
    ) as c:
        yield c


def _keycloak_login(authorize_url: str, username: str, password: str) -> httpx.Response:
    """Submit Keycloak's login form; return the final redirect response (to our callback)."""
    with httpx.Client(follow_redirects=False, timeout=10) as kc:
        page = kc.get(authorize_url)
        assert page.status_code == 200, page.text[:200]
        m = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
        assert m, "Keycloak login form not found"
        action = html.unescape(m.group(1))
        # Keycloak sets Secure cookies; browsers send them to http://localhost (a secure
        # context) but Python's cookie jar does not, so forward them the way a browser would.
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in kc.cookies.jar)
        return kc.post(
            action,
            data={"username": username, "password": password, "credentialId": ""},
            headers={"Cookie": cookie_header},
        )


def _start_login(client: TestClient, next_path: str = "/cases") -> str:
    r = client.get(f"/auth/login?next={next_path}")
    assert r.status_code in (302, 307)
    url = r.headers["location"]
    q = parse_qs(urlparse(url).query)
    # PKCE S256, nonce and state must be present on the authorization request.
    assert q["code_challenge_method"] == ["S256"]
    assert len(q["code_challenge"][0]) >= 43
    assert q["nonce"] and q["state"]
    assert q["redirect_uri"] == ["http://localhost:5173/auth/callback"]
    return url


def test_full_login_creates_server_side_session(client: TestClient, settings: Settings) -> None:
    password = settings.demo_user_password.get_secret_value()  # type: ignore[union-attr]
    authorize_url = _start_login(client)
    kc = _keycloak_login(authorize_url, "furn-investigator", password)
    assert kc.status_code == 302
    callback = urlparse(kc.headers["location"])
    assert callback.path == "/auth/callback"
    r = client.get(f"/auth/callback?{callback.query}")
    assert r.status_code == 303 and r.headers["location"] == "/cases"
    set_cookie = r.headers["set-cookie"].lower()
    assert (
        "opsbridge_session=" in set_cookie
        and "httponly" in set_cookie
        and "samesite=lax" in set_cookie
    )
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "furn-investigator"
    assert [(m["tenant_slug"], m["roles"]) for m in body["memberships"]] == [
        ("demo-furniture", ["investigator"])
    ]

    # Logout revokes the server-side session and returns an RP-initiated logout URL.
    out = client.post("/auth/logout", headers={"X-CSRF-Token": body["csrf_token"]})
    assert out.status_code == 200
    assert out.json()["logout_url"].startswith(settings.oidc_issuer)
    assert client.get("/api/v1/me").status_code == 401


def test_authorization_code_cannot_be_replayed(client: TestClient, settings: Settings) -> None:
    password = settings.demo_user_password.get_secret_value()  # type: ignore[union-attr]
    kc = _keycloak_login(_start_login(client), "furn-requester", password)
    query = urlparse(kc.headers["location"]).query
    assert client.get(f"/auth/callback?{query}").status_code == 303
    client.cookies.clear()
    # Same code + state again: state was consumed and the code is single-use.
    assert client.get(f"/auth/callback?{query}").status_code == 401


def test_tampered_state_is_rejected(client: TestClient, settings: Settings) -> None:
    password = settings.demo_user_password.get_secret_value()  # type: ignore[union-attr]
    kc = _keycloak_login(_start_login(client), "furn-requester", password)
    q = parse_qs(urlparse(kc.headers["location"]).query)
    r = client.get(f"/auth/callback?code={q['code'][0]}&state=forged-state")
    assert r.status_code == 401
    assert "opsbridge_session" not in r.headers.get("set-cookie", "")


def test_wrong_password_never_reaches_callback(client: TestClient) -> None:
    kc = _keycloak_login(_start_login(client), "furn-requester", "definitely-wrong")
    assert kc.status_code == 200  # Keycloak re-renders the form; no code is issued
    assert "code=" not in kc.headers.get("location", "")


def test_open_redirect_in_next_is_neutralized(client: TestClient, settings: Settings) -> None:
    password = settings.demo_user_password.get_secret_value()  # type: ignore[union-attr]
    kc = _keycloak_login(
        _start_login(client, next_path="//evil.example/steal"), "furn-requester", password
    )
    r = client.get(f"/auth/callback?{urlparse(kc.headers['location']).query}")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_user_without_membership_gets_no_tenant_access(
    client: TestClient, settings: Settings
) -> None:
    # it-requester is a member of demo-it only.
    password = settings.demo_user_password.get_secret_value()  # type: ignore[union-attr]
    kc = _keycloak_login(_start_login(client), "it-requester", password)
    client.get(f"/auth/callback?{urlparse(kc.headers['location']).query}")
    assert client.get("/api/v1/tenants/demo-furniture/cases").status_code == 404
    assert client.get("/api/v1/tenants/demo-it/cases").status_code == 200


def test_machine_identity_token_is_audience_scoped(settings: Settings) -> None:
    """Worker client-credentials token: verifiable via JWKS, audience-restricted, no user."""
    secret = settings.worker_client_secret.get_secret_value()
    token_url = f"{settings.oidc_issuer}/protocol/openid-connect/token"
    r = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": "opsbridge-worker",
            "client_secret": secret,
        },
        timeout=10,
    )
    assert r.status_code == 200
    access = r.json()["access_token"]
    jwks = jwt.PyJWKClient(f"{settings.oidc_issuer}/protocol/openid-connect/certs")
    key = jwks.get_signing_key_from_jwt(access)
    claims = jwt.decode(
        access,
        key.key,
        algorithms=["RS256"],
        audience="opsbridge-records-mcp",
        issuer=settings.oidc_issuer,
    )
    assert claims["azp"] == "opsbridge-worker"
    assert "preferred_username" not in claims or claims["preferred_username"].startswith(
        "service-account-"
    )
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(
            access,
            key.key,
            algorithms=["RS256"],
            audience="opsbridge-web",
            issuer=settings.oidc_issuer,
        )
    bad = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": "opsbridge-worker",
            "client_secret": "wrong",
        },
        timeout=10,
    )
    assert bad.status_code == 401
