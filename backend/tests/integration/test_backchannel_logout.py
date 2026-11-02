"""A session ended at the identity provider ends in OpsBridge (OpenID Connect Back-Channel
Logout 1.0; production review A16).

Before, a person signed out by an administrator at the identity provider kept OpsBridge until
the session went idle or reached its absolute limit (30 minutes, 8 hours). Keycloak now sends
a logout token to /auth/backchannel-logout, and OpsBridge revokes the sessions it opened from
that identity-provider session.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from opsbridge import cli
from opsbridge.api.app import create_app
from opsbridge.auth.backchannel import BACKCHANNEL_EVENT
from opsbridge.auth.sessions import COOKIE_NAME, create_session, hash_token
from opsbridge.config import REPO_ROOT, Settings, get_settings
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.persistence.models import UserSession
from opsbridge.seed import seed_demo

from .conftest import make_user
from .servers import serve
from .test_oidc_login import _keycloak_login, _start_login

KEYCLOAK = "http://localhost:8081"
REALM = "opsbridge"
# How the Keycloak container reaches this host: Podman maps the name itself; Docker on Linux
# needs the compose file's extra_hosts entry.
HOST_FROM_KEYCLOAK = os.environ.get("OPSBRIDGE_TEST_HOST_FROM_KEYCLOAK", "host.docker.internal")


def _env(name: str) -> str | None:
    if name in os.environ:
        return os.environ[name]
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None


@pytest.fixture
def kc_settings() -> Settings:
    s = get_settings()
    if s.demo_user_password is None or _env("KEYCLOAK_ADMIN_PASSWORD") is None:
        pytest.skip("demo user and Keycloak admin passwords not configured (run make setup)")
    return s.model_copy(update={"rate_limit_per_minute": 1000})


@pytest.fixture
def admin() -> Iterator[httpx.Client]:
    token = httpx.post(
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": _env("KEYCLOAK_ADMIN_PASSWORD"),
        },
        timeout=10,
    ).json()["access_token"]
    with httpx.Client(
        base_url=f"{KEYCLOAK}/admin/realms/{REALM}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ) as c:
        yield c


@pytest.fixture
def backchannel_to(admin: httpx.Client, kc_settings: Settings) -> Iterator[Any]:
    """Points the web client's back-channel logout URL at a URL for the test, then puts back
    exactly what was there (Keycloak keeps an attribute that an update leaves out, and drops
    one set to the empty string)."""
    client = admin.get("/clients", params={"clientId": kc_settings.oidc_client_id}).json()[0]
    keys = ("backchannel.logout.url", "backchannel.logout.session.required")
    before = {k: client.get("attributes", {}).get(k, "") for k in keys}

    def point(url: str) -> None:
        current = admin.get(f"/clients/{client['id']}").json()
        attributes = {**current["attributes"], keys[0]: url, keys[1]: "true"}
        assert (
            admin.put(
                f"/clients/{client['id']}", json={**current, "attributes": attributes}
            ).status_code
            == 204
        )

    yield point
    current = admin.get(f"/clients/{client['id']}").json()
    admin.put(
        f"/clients/{client['id']}",
        json={**current, "attributes": {**current["attributes"], **before}},
    )


@pytest.fixture
def seeded(kc_settings: Settings) -> None:
    db = Database(make_engine("migrator", database="opsbridge_test"))
    seed_demo(db, kc_settings)
    db.engine.dispose()


def _signed_in(app: Any, settings: Settings, username: str) -> TestClient:
    client = TestClient(app, base_url="http://localhost:5173", follow_redirects=False)
    password = settings.demo_user_password.get_secret_value()  # type: ignore[union-attr]
    kc = _keycloak_login(_start_login(client), username, password)
    assert client.get(f"/auth/callback?{urlparse(kc.headers['location']).query}").status_code == 303
    assert client.get("/api/v1/me").status_code == 200
    return client


def _user_id(admin: httpx.Client, username: str) -> str:
    return str(admin.get("/users", params={"username": username, "exact": "true"}).json()[0]["id"])


@pytest.mark.integration
@pytest.mark.keycloak
def test_signing_a_person_out_at_keycloak_ends_their_opsbridge_sessions(
    app_db: Database, kc_settings: Settings, seeded: None, admin: httpx.Client, backchannel_to: Any
) -> None:
    settings = kc_settings
    app = create_app(settings=settings, db=app_db)
    with serve(app, host="0.0.0.0") as base:  # Keycloak calls in from a container
        backchannel_to(f"http://{HOST_FROM_KEYCLOAK}:{urlparse(base).port}/auth/backchannel-logout")
        auditor = _signed_in(app, settings, "furn-auditor")
        investigator = _signed_in(app, settings, "furn-investigator")

        assert admin.post(f"/users/{_user_id(admin, 'furn-auditor')}/logout").status_code == 204
        deadline = time.monotonic() + 10
        while auditor.get("/api/v1/me").status_code == 200 and time.monotonic() < deadline:
            time.sleep(0.2)

        assert auditor.get("/api/v1/me").status_code == 401
        assert investigator.get("/api/v1/me").status_code == 200  # someone else's stays


def _idp_session(app_db: Database, client: TestClient) -> str | None:
    h = hash_token(client.cookies[COOKIE_NAME])
    with app_db.session(DbContext(session_hash=h)) as s:
        row = s.get(UserSession, h)
        return row.idp_session_id if row else None


@pytest.mark.integration
@pytest.mark.keycloak
def test_ending_one_keycloak_session_ends_only_the_opsbridge_session_opened_from_it(
    app_db: Database, kc_settings: Settings, seeded: None, admin: httpx.Client, backchannel_to: Any
) -> None:
    app = create_app(settings=kc_settings, db=app_db)
    with serve(app, host="0.0.0.0") as base:  # Keycloak calls in from a container
        backchannel_to(f"http://{HOST_FROM_KEYCLOAK}:{urlparse(base).port}/auth/backchannel-logout")
        laptop = _signed_in(app, kc_settings, "furn-auditor")
        phone = _signed_in(app, kc_settings, "furn-auditor")
        laptop_sid, phone_sid = _idp_session(app_db, laptop), _idp_session(app_db, phone)
        assert laptop_sid and phone_sid and laptop_sid != phone_sid  # recorded at sign-in

        assert admin.delete(f"/sessions/{laptop_sid}").status_code == 204
        deadline = time.monotonic() + 10
        while laptop.get("/api/v1/me").status_code == 200 and time.monotonic() < deadline:
            time.sleep(0.2)

        assert laptop.get("/api/v1/me").status_code == 401
        assert phone.get("/api/v1/me").status_code == 200


# --- Logout tokens signed here, against the provider's keys as the API fetched them. ---

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROTATED = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(*keys: tuple[str, Any]) -> dict[str, Any]:
    return {
        "keys": [
            {**jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key(), as_dict=True), "kid": kid,
             "use": "sig", "alg": "RS256"}
            for kid, k in keys
        ]
    }  # fmt: skip


def _logout_token(settings: Settings, *, key: Any = KEY, kid: str = "k1", **claims: Any) -> str:
    now = int(time.time())
    body = {
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_client_id,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 120,
        "events": {BACKCHANNEL_EVENT: {}},
        **claims,
    }
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid, "typ": "logout+jwt"})


@pytest.fixture
def provider(app_db: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The API, with the identity provider's published keys replaced by `provider.keys`;
    `provider.fetches` records each fetch and whether it was forced."""
    settings = get_settings()
    app = create_app(settings=settings, db=app_db)
    fake = SimpleNamespace(keys=_jwks(("k1", KEY)), fetches=[], settings=settings, client=None)

    async def fetch_jwk_set(force: bool = False) -> dict[str, Any]:
        fake.fetches.append(force)
        return fake.keys

    with TestClient(app, base_url="http://localhost:5173") as client:
        monkeypatch.setattr(app.state.oauth.opsbridge, "fetch_jwk_set", fetch_jwk_set)
        fake.client = client
        yield fake


def _person(engine: Engine, settings: Settings, name: str) -> tuple[uuid.UUID, str]:
    uid = make_user(engine, name, issuer=settings.oidc_issuer)
    with engine.begin() as conn:
        subject = conn.execute(
            text("SELECT subject FROM users WHERE id = :u"), {"u": uid}
        ).scalar_one()
    return uid, str(subject)


def _live(app_db: Database, h: str) -> bool:
    with app_db.session(DbContext(session_hash=h)) as s:
        row = s.get(UserSession, h)
        return row is not None and row.revoked_at is None


def _logout(provider: Any, token: str | None) -> httpx.Response:
    data = {} if token is None else {"logout_token": token}
    return provider.client.post("/auth/backchannel-logout", data=data)


@pytest.mark.integration
def test_a_logout_ends_the_sessions_opened_from_that_provider_session_and_no_others(
    app_db: Database, migrator_engine: Engine, provider: Any
) -> None:
    s = provider.settings
    pat, pat_sub = _person(migrator_engine, s, "pat")
    lee, _ = _person(migrator_engine, s, "lee")

    def session(user: uuid.UUID, sid: str | None) -> str:
        return hash_token(create_session(app_db, s, user, None, sid))

    laptop, phone, before_upgrade = session(pat, "sid-a"), session(pat, "sid-b"), session(pat, None)
    other = session(lee, "sid-c")

    r = _logout(provider, _logout_token(s, sub=pat_sub, sid="sid-a"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    # The session recorded without a sid cannot be told apart from the ended one, so it ends.
    assert [_live(app_db, h) for h in (laptop, phone, before_upgrade, other)] == [
        False, True, False, True
    ]  # fmt: skip


@pytest.mark.integration
def test_a_logout_naming_only_the_person_or_only_the_session(
    app_db: Database, migrator_engine: Engine, provider: Any
) -> None:
    s = provider.settings
    pat, pat_sub = _person(migrator_engine, s, "pat")
    lee, _ = _person(migrator_engine, s, "lee")
    a = hash_token(create_session(app_db, s, pat, None, "sid-a"))
    unrecorded = hash_token(create_session(app_db, s, pat, None, None))
    other = hash_token(create_session(app_db, s, lee, None, "sid-c"))

    assert _logout(provider, _logout_token(s, sid="sid-a")).status_code == 200
    # A sid alone cannot be tied to a session recorded without one.
    assert [_live(app_db, h) for h in (a, unrecorded, other)] == [False, True, True]

    assert _logout(provider, _logout_token(s, sub=pat_sub)).status_code == 200
    assert [_live(app_db, h) for h in (unrecorded, other)] == [False, True]


@pytest.mark.integration
def test_a_refused_or_unknown_logout_ends_nothing(
    app_db: Database, migrator_engine: Engine, provider: Any
) -> None:
    s = provider.settings
    pat, pat_sub = _person(migrator_engine, s, "pat")
    h = hash_token(create_session(app_db, s, pat, None, "sid-a"))
    forged = _logout_token(s, key=ROTATED, kid="k1", sub=pat_sub, sid="sid-a")
    for token in (forged, _logout_token(s, sub=pat_sub, nonce="n"), "garbage", None):
        r = _logout(provider, token)
        assert (r.status_code, r.json()) == (400, {"error": "invalid_request"})
    assert (
        _logout(provider, _logout_token(s, sub="never-signed-in", sid="sid-a")).status_code == 200
    )
    assert _live(app_db, h)


@pytest.mark.integration
def test_an_unknown_key_fetches_the_keys_again_but_not_more_than_once_a_minute(
    app_db: Database, migrator_engine: Engine, provider: Any
) -> None:
    s = provider.settings
    pat, pat_sub = _person(migrator_engine, s, "pat")
    h = hash_token(create_session(app_db, s, pat, None, "sid-a"))
    provider.keys = _jwks(("k1", KEY), ("k2", ROTATED))  # what a forced fetch now returns
    cached = _jwks(("k1", KEY))

    async def fetch_jwk_set(force: bool = False) -> dict[str, Any]:
        provider.fetches.append(force)
        return provider.keys if force else cached

    provider.client.app.state.oauth.opsbridge.fetch_jwk_set = fetch_jwk_set
    rotated = _logout_token(s, key=ROTATED, kid="k2", sub=pat_sub, sid="sid-a")
    assert _logout(provider, rotated).status_code == 200 and not _live(app_db, h)
    assert provider.fetches == [False, True]

    unknown = _logout_token(s, key=ROTATED, kid="k3", sub=pat_sub)
    for _ in range(5):
        assert _logout(provider, unknown).status_code == 400
    assert provider.fetches.count(True) == 1  # no further forced fetch within the minute


@pytest.mark.integration
def test_the_revoking_role_reads_no_tokens_and_only_the_api_may_call_it(
    migrator_engine: Engine,
) -> None:
    fn = "ops.revoke_idp_sessions(uuid, text)"
    with migrator_engine.connect() as conn:

        def q(sql: str) -> Any:
            return conn.execute(text(sql)).scalar_one()

        assert q("SELECT rolcanlogin OR rolbypassrls FROM pg_roles "
                 "WHERE rolname = 'opsbridge_session_revoker'") is False  # fmt: skip
        for column in ("id_token", "csrf_token"):
            assert q(f"SELECT has_column_privilege('opsbridge_session_revoker', "
                     f"'user_sessions', '{column}', 'SELECT')") is False  # fmt: skip
        assert q("SELECT has_column_privilege('opsbridge_session_revoker', 'user_sessions', "
                 "'expires_at', 'UPDATE')") is False  # fmt: skip
        assert (
            q("SELECT has_table_privilege('opsbridge_session_revoker', 'users', 'SELECT')") is False
        )
        for role, allowed in (("opsbridge_app", True), ("opsbridge_worker", False),
                              ("opsbridge_mcp", False)):  # fmt: skip
            assert q(f"SELECT has_function_privilege('{role}', '{fn}', 'EXECUTE')") is allowed


@pytest.mark.integration
def test_an_operator_can_end_a_persons_sessions_when_the_provider_could_not_reach_the_api(
    app_db: Database, migrator_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keycloak does not retry a back-channel logout the API missed (docs/operations.md)."""
    s = get_settings()
    pat, pat_sub = _person(migrator_engine, s, "pat")
    lee, _ = _person(migrator_engine, s, "lee")
    sessions = [hash_token(create_session(app_db, s, pat, None, sid)) for sid in ("a", None)]
    other = hash_token(create_session(app_db, s, lee, None, "c"))

    def end(subject: str) -> str:
        argv = ["end-sessions", "--subject", subject, "--database", "opsbridge_test"]
        assert cli.main(argv) == 0
        return capsys.readouterr().out

    assert "ended 2 live session(s)" in end(pat_sub)
    assert not any(_live(app_db, h) for h in sessions) and _live(app_db, other)
    assert "ended 0 live session(s)" in end("never-signed-in")
