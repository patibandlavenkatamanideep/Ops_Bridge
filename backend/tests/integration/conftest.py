from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from opsbridge.config import get_settings
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.persistence.models import Base

TEST_DB = "opsbridge_test"


@pytest.fixture(scope="session")
def migrator_engine() -> Iterator[Engine]:
    engine = make_engine("migrator", database=TEST_DB)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_db() -> Iterator[Database]:
    db = Database(make_engine("app", database=TEST_DB))
    yield db
    db.engine.dispose()


@pytest.fixture(scope="session")
def worker_db() -> Iterator[Database]:
    db = Database(make_engine("worker", database=TEST_DB))
    yield db
    db.engine.dispose()


@pytest.fixture(scope="session")
def mcp_db() -> Iterator[Database]:
    db = Database(make_engine("mcp", database=TEST_DB))
    yield db
    db.engine.dispose()


@pytest.fixture(autouse=True)
def clean_db(request: pytest.FixtureRequest) -> Iterator[None]:
    if "integration" not in request.keywords:
        yield
        return
    engine: Engine = request.getfixturevalue("migrator_engine")
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


def make_tenant(
    engine: Engine, slug: str, pack: str = "furniture", tier: str = "standard"
) -> uuid.UUID:
    tid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, tier, pack_id, synthetic_demo) "
                "VALUES (:id, :slug, :name, :tier, :pack, true)"
            ),
            {"id": tid, "slug": slug, "name": slug.title(), "tier": tier, "pack": pack},
        )
    return tid


def make_user(
    engine: Engine, username: str, issuer: str = "https://idp.test/realms/x"
) -> uuid.UUID:
    uid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, issuer, subject, username, display_name, email) "
                "VALUES (:id, :iss, :sub, :u, :u, :email)"
            ),
            {
                "id": uid,
                "iss": issuer,
                "sub": str(uuid.uuid4()),
                "u": username,
                "email": f"{username}@example.test",
            },
        )
    return uid


def add_member(
    engine: Engine,
    tenant: uuid.UUID,
    user: uuid.UUID,
    roles: list[str],
    scope: list[str] | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, roles, approval_scope, status) "
                "VALUES (:id, :t, :u, :r, :s, 'active')"
            ),
            {"id": uuid.uuid4(), "t": tenant, "u": user, "r": roles, "s": scope or []},
        )


def ctx(tenant: uuid.UUID | None, user: uuid.UUID | None = None) -> DbContext:
    return DbContext(tenant_id=tenant, user_id=user)


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    return get_settings()
