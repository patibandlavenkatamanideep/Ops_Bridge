"""PostgreSQL row-level security against the real database roles (OB-10)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from opsbridge.persistence.db import Database, DbContext

from .conftest import add_member, ctx, make_tenant, make_user

pytestmark = pytest.mark.integration


def _insert_case(db: Database, tenant: uuid.UUID, user: uuid.UUID, number: str) -> uuid.UUID:
    cid = uuid.uuid4()
    with db.session(ctx(tenant, user)) as s:
        s.execute(
            text(
                "INSERT INTO cases (id, tenant_id, case_number, title, description, source_key, "
                "discrepancy_types, evidence_readiness, status, requester_id, row_version) "
                "VALUES (:id, :t, :n, 'title', '', :sk, '{}', 'not_collected', 'new', :u, 1)"
            ),
            {"id": cid, "t": tenant, "n": number, "sk": f"invoice:{number}", "u": user},
        )
    return cid


def _count_cases(db: Database, c: DbContext | None) -> int:
    with db.session(c) as s:
        return int(s.execute(text("SELECT count(*) FROM cases")).scalar_one())


@pytest.fixture
def two_tenants(migrator_engine: Engine, app_db: Database):  # type: ignore[no-untyped-def]
    a = make_tenant(migrator_engine, "tenant-a")
    b = make_tenant(migrator_engine, "tenant-b", pack="it_hardware")
    ua = make_user(migrator_engine, "user-a")
    ub = make_user(migrator_engine, "user-b")
    add_member(migrator_engine, a, ua, ["requester"])
    add_member(migrator_engine, b, ub, ["requester"])
    case_a = _insert_case(app_db, a, ua, "A-1")
    case_b = _insert_case(app_db, b, ub, "B-1")
    return a, b, ua, ub, case_a, case_b


def test_roles_are_not_privileged(app_db: Database, worker_db: Database, mcp_db: Database) -> None:
    for db in (app_db, worker_db, mcp_db):
        with db.session() as s:
            row = s.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
            assert tuple(row) == (False, False)
            owner = s.execute(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'cases'")
            ).scalar_one()
            assert owner == "opsbridge_migrator"


def test_no_context_sees_nothing(app_db: Database, two_tenants) -> None:  # type: ignore[no-untyped-def]
    assert _count_cases(app_db, None) == 0


def test_tenant_context_sees_only_own_rows(app_db: Database, two_tenants) -> None:  # type: ignore[no-untyped-def]
    a, b, ua, ub, case_a, case_b = two_tenants
    with app_db.session(ctx(a, ua)) as s:
        ids = {r[0] for r in s.execute(text("SELECT id FROM cases"))}
        assert ids == {case_a}
        # Direct lookup of the other tenant's ID returns nothing (no existence oracle).
        assert (
            s.execute(text("SELECT id FROM cases WHERE id = :id"), {"id": case_b}).first() is None
        )


def test_insert_into_other_tenant_is_rejected(app_db: Database, two_tenants) -> None:  # type: ignore[no-untyped-def]
    a, b, ua, _ub, _ca, _cb = two_tenants
    with pytest.raises(ProgrammingError, match="row-level security"):
        with app_db.session(ctx(a, ua)) as s:
            s.execute(
                text(
                    "INSERT INTO cases (id, tenant_id, case_number, title, description, source_key, "
                    "discrepancy_types, evidence_readiness, status, requester_id, row_version) "
                    "VALUES (:id, :t, 'X-1', 't', '', 'k', '{}', 'not_collected', 'new', :u, 1)"
                ),
                {"id": uuid.uuid4(), "t": b, "u": ua},
            )


def test_update_cannot_move_row_to_other_tenant(app_db: Database, two_tenants) -> None:  # type: ignore[no-untyped-def]
    a, b, ua, _ub, case_a, _cb = two_tenants
    with pytest.raises(ProgrammingError, match="row-level security"):
        with app_db.session(ctx(a, ua)) as s:
            s.execute(
                text("UPDATE cases SET tenant_id = :b WHERE id = :id"), {"b": b, "id": case_a}
            )


def test_composite_fk_blocks_cross_tenant_reference(
    migrator_engine: Engine,
    app_db: Database,
    two_tenants,  # type: ignore[no-untyped-def]
) -> None:
    a, b, ua, ub, case_a, case_b = two_tenants
    # Even if a caller guessed tenant B's case id, a tenant-A row cannot reference it:
    # the FK is (tenant_id, case_id) → cases(tenant_id, id).
    with pytest.raises((IntegrityError, ProgrammingError)), app_db.session(ctx(a, ua)) as s:
        s.execute(
            text(
                "INSERT INTO conversation_messages (id, tenant_id, case_id, seq, author_kind, "
                "author_id, body, body_sha256) VALUES (:id, :t, :c, 1, 'user', :u, 'hi', 'x')"
            ),
            {"id": uuid.uuid4(), "t": a, "c": case_b, "u": ua},
        )


def test_pooled_connection_does_not_inherit_tenant(migrator_engine: Engine, two_tenants) -> None:  # type: ignore[no-untyped-def]
    a, b, ua, ub, case_a, case_b = two_tenants
    from opsbridge.persistence.db import make_engine

    engine = make_engine("app", database="opsbridge_test")
    engine.pool.dispose()
    single = Database(engine)
    # Force a pool of exactly one connection so every session reuses it.
    from sqlalchemy.pool import QueuePool

    single.engine.pool = QueuePool(single.engine.pool._creator, pool_size=1, max_overflow=0)  # type: ignore[attr-defined]
    pids = []
    with single.session(ctx(a, ua)) as s:
        pids.append(s.execute(text("SELECT pg_backend_pid()")).scalar_one())
        assert {r[0] for r in s.execute(text("SELECT id FROM cases"))} == {case_a}
    with single.session(ctx(b, ub)) as s:
        pids.append(s.execute(text("SELECT pg_backend_pid()")).scalar_one())
        assert {r[0] for r in s.execute(text("SELECT id FROM cases"))} == {case_b}
    with single.session(None) as s:
        pids.append(s.execute(text("SELECT pg_backend_pid()")).scalar_one())
        assert s.execute(text("SELECT count(*) FROM cases")).scalar_one() == 0
        assert s.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar_one() == ""
    assert len(set(pids)) == 1, "test must reuse one pooled connection"

    # An error mid-transaction rolls back; the next borrower still has no tenant.
    with pytest.raises(DBAPIError), single.session(ctx(a, ua)) as s:
        s.execute(text("SELECT 1/0"))
    with single.session(None) as s:
        assert s.execute(text("SELECT count(*) FROM cases")).scalar_one() == 0
    single.engine.dispose()


def test_context_reapplied_after_commit_within_one_session(app_db: Database, two_tenants) -> None:  # type: ignore[no-untyped-def]
    a, _b, ua, _ub, case_a, _cb = two_tenants
    with app_db.session(ctx(a, ua)) as s:
        assert s.execute(text("SELECT count(*) FROM cases")).scalar_one() == 1
        s.commit()  # new transaction starts on next statement and must get context again
        assert s.execute(text("SELECT count(*) FROM cases")).scalar_one() == 1


def test_worker_claims_jobs_across_tenants_but_not_business_rows(
    worker_db: Database,
    app_db: Database,
    two_tenants,  # type: ignore[no-untyped-def]
) -> None:
    a, b, ua, ub, _ca, _cb = two_tenants
    for tenant, user in ((a, ua), (b, ub)):
        with app_db.session(ctx(tenant, user)) as s:
            s.execute(
                text(
                    "INSERT INTO jobs (id, tenant_id, kind, payload, status, priority, attempts, "
                    "max_attempts, fencing_token) VALUES (:id, :t, 'noop', '{}', 'queued', 100, 0, 5, 0)"
                ),
                {"id": uuid.uuid4(), "t": tenant},
            )
    with worker_db.session(None) as s:
        assert s.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 2
        assert s.execute(text("SELECT count(*) FROM cases")).scalar_one() == 0
    with worker_db.session(ctx(a)) as s:
        assert s.execute(text("SELECT count(*) FROM cases")).scalar_one() == 1
    # The API role only sees its own tenant's jobs.
    with app_db.session(ctx(a, ua)) as s:
        assert s.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1


def test_audit_events_are_append_only_for_application_roles(app_db: Database, two_tenants) -> None:  # type: ignore[no-untyped-def]
    a, _b, ua, _ub, _ca, _cb = two_tenants
    with app_db.session(ctx(a, ua)) as s:
        s.execute(
            text(
                "INSERT INTO audit_events (tenant_id, actor_kind, actor_id, action, resource_type, "
                "resource_id, outcome, details) VALUES (:t, 'user', 'u', 'test', 'case', 'x', 'ok', '{}')"
            ),
            {"t": a},
        )
    for stmt in ("UPDATE audit_events SET action = 'tampered'", "DELETE FROM audit_events"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with app_db.session(ctx(a, ua)) as s:
                s.execute(text(stmt))


def test_membership_visibility(app_db: Database, migrator_engine: Engine, two_tenants) -> None:  # type: ignore[no-untyped-def]
    a, b, ua, ub, _ca, _cb = two_tenants
    # Before a tenant is chosen, a user sees only their own memberships and tenants.
    with app_db.session(DbContext(user_id=ua)) as s:
        tenants = {r[0] for r in s.execute(text("SELECT id FROM tenants"))}
        assert tenants == {a}
        assert {r[0] for r in s.execute(text("SELECT tenant_id FROM memberships"))} == {a}
    # A tenant-A user cannot add themselves to tenant B.
    with pytest.raises(ProgrammingError, match="row-level security"):
        with app_db.session(ctx(a, ua)) as s:
            s.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, roles, approval_scope, status) "
                    "VALUES (:id, :b, :u, '{approver}', '{}', 'active')"
                ),
                {"id": uuid.uuid4(), "b": b, "u": ua},
            )


def test_sessions_visible_only_by_hash(app_db: Database, two_tenants) -> None:  # type: ignore[no-untyped-def]
    _a, _b, ua, _ub, _ca, _cb = two_tenants
    h1, h2 = "a" * 64, "b" * 64
    for h in (h1, h2):
        with app_db.session(DbContext(session_hash=h)) as s:
            s.execute(
                text(
                    "INSERT INTO user_sessions (id, user_id, csrf_token, expires_at, idle_expires_at) "
                    "VALUES (:h, :u, 'csrf', now() + interval '1 hour', now() + interval '30 minutes')"
                ),
                {"h": h, "u": ua},
            )
    with app_db.session(DbContext(session_hash=h1)) as s:
        assert [r[0] for r in s.execute(text("SELECT id FROM user_sessions"))] == [h1]
    with app_db.session(None) as s:
        assert s.execute(text("SELECT count(*) FROM user_sessions")).scalar_one() == 0
