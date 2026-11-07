"""Ended sessions are removed, and the ID tokens they hold with them (production review A16).

Before, a session that expired, went idle or was revoked stayed in `user_sessions` for good,
and each row keeps the identity provider's ID token (name, e-mail and other claims). Nothing
removed them. The worker now prunes sessions that ended more than a grace period ago, through
`ops.prune_sessions` (SECURITY DEFINER, owned by a NOLOGIN role that may only read and delete
sessions); a session still active, or ended within the grace period, stays.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text, update
from sqlalchemy.exc import DBAPIError

from opsbridge.auth.sessions import create_session, hash_token, prune_sessions
from opsbridge.config import get_settings
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import UserSession

from .conftest import make_user

pytestmark = pytest.mark.integration
ID_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJuYW1lIjoiUGF0IFBlcnNvbiIsImVtYWlsIjoicEBleGFtcGxlLnRlc3QifQ.sig"


def test_sessions_that_ended_before_the_grace_period_are_pruned(
    app_db: Database, worker_db: Database, migrator_engine: Engine
) -> None:
    uid = make_user(migrator_engine, "pruned-person")
    settings = get_settings()
    now = datetime.now(UTC)

    def session(**ended: datetime) -> str:
        h = hash_token(create_session(app_db, settings, uid, ID_TOKEN))
        if ended:
            with app_db.session(DbContext(session_hash=h)) as s:
                s.execute(update(UserSession).where(UserSession.id == h).values(**ended))
        return h

    def present(h: str) -> bool:
        with app_db.session(DbContext(session_hash=h)) as s:
            return s.get(UserSession, h) is not None

    active = session()
    long_ago = now - timedelta(days=3)
    expired = session(expires_at=long_ago, idle_expires_at=long_ago)
    idle = session(idle_expires_at=long_ago)
    revoked = session(revoked_at=long_ago)
    recent = session(idle_expires_at=now - timedelta(minutes=5))  # within the grace period

    assert prune_sessions(worker_db, timedelta(hours=24)) == 3
    assert present(active) and present(recent)
    assert not any(present(h) for h in (expired, idle, revoked))
    assert prune_sessions(worker_db, timedelta(hours=24)) == 0  # nothing more to remove


@pytest.mark.parametrize("keep", ["-100 years", "1 day -25 hours", None])
def test_the_function_refuses_a_period_that_would_reach_live_sessions(
    app_db: Database, worker_db: Database, migrator_engine: Engine, keep: str | None
) -> None:
    """Python checks the setting (above 0), but the function is what the worker may execute:
    `ops.prune_sessions('-100 years')` deleted every session, live ones included, as the
    pruning role, which may delete any tenant's (review G: maintenance functions validate
    their own arguments)."""
    uid = make_user(migrator_engine, "live-person")
    h = hash_token(create_session(app_db, get_settings(), uid, ID_TOKEN))
    with pytest.raises(DBAPIError, match="keep must be a non-negative interval"):
        with worker_db.session(DbContext()) as s:
            s.execute(text("SELECT ops.prune_sessions(CAST(:keep AS interval))"), {"keep": keep})
    with app_db.session(DbContext(session_hash=h)) as s:
        assert s.get(UserSession, h) is not None  # the live session is still there
