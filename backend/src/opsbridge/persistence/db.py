"""Database engines and tenant-scoped sessions.

Each role (app, worker, mcp) has its own engine and connection pool. Request and job
context (tenant, user, session hash, OIDC identity) is stored in `Session.info` and
applied with transaction-local `set_config(..., true)` at the start of EVERY
transaction via an `after_begin` hook. Transaction-local settings vanish at COMMIT or
ROLLBACK, so a pooled connection never carries a previous borrower's tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from opsbridge.config import DbRole, Settings, get_settings


@dataclass(frozen=True)
class DbContext:
    tenant_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    session_hash: str | None = None
    oidc_issuer: str | None = None
    oidc_subject: str | None = None
    invite_token_hash: str | None = None  # finds one invitation before its tenant is known

    def settings(self) -> dict[str, str]:
        return {
            "app.tenant_id": str(self.tenant_id) if self.tenant_id else "",
            "app.user_id": str(self.user_id) if self.user_id else "",
            "app.session_hash": self.session_hash or "",
            "app.oidc_issuer": self.oidc_issuer or "",
            "app.oidc_subject": self.oidc_subject or "",
            "app.invite_token_hash": self.invite_token_hash or "",
        }


_CONTEXT_KEY = "opsbridge_db_context"
_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant, true), set_config('app.user_id', :user, true),"
    " set_config('app.session_hash', :session_hash, true),"
    " set_config('app.oidc_issuer', :issuer, true), set_config('app.oidc_subject', :subject, true),"
    " set_config('app.invite_token_hash', :invite, true)"
)


@event.listens_for(Session, "after_begin")
def _apply_context(session: Session, _transaction: Any, connection: Any) -> None:
    ctx: DbContext = session.info.get(_CONTEXT_KEY, DbContext())
    s = ctx.settings()
    connection.execute(
        _SET_CONTEXT,
        {
            "tenant": s["app.tenant_id"],
            "user": s["app.user_id"],
            "session_hash": s["app.session_hash"],
            "issuer": s["app.oidc_issuer"],
            "subject": s["app.oidc_subject"],
            "invite": s["app.invite_token_hash"],
        },
    )


def make_engine(
    role: DbRole, settings: Settings | None = None, database: str | None = None
) -> Engine:
    s = settings or get_settings()
    return create_engine(
        s.database_url(role, database),
        pool_size=s.db_pool_size,
        max_overflow=2,
        pool_pre_ping=True,
        connect_args={
            "options": f"-c statement_timeout={s.db_statement_timeout_ms}",
            "application_name": f"opsbridge-{role}",
        },
    )


class Database:
    """Engine + session factory for one database role."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def session(self, ctx: DbContext | None = None) -> Iterator[Session]:
        """Yield a session whose every transaction carries `ctx`. Commits on success."""
        session = self._factory()
        session.info[_CONTEXT_KEY] = ctx or DbContext()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def set_context(self, session: Session, ctx: DbContext) -> None:
        """Change context for the NEXT transaction (call after commit)."""
        session.info[_CONTEXT_KEY] = ctx


@lru_cache(maxsize=8)
def get_database(role: DbRole) -> Database:
    return Database(make_engine(role))
