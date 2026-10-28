"""Server-side browser sessions.

The cookie holds an opaque random token. Only its SHA-256 is stored, so a database
read does not yield a usable cookie. Lookups run with `app.session_hash` set, which
is the only way the RLS policy exposes a session row.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from opsbridge.config import Settings
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import User, UserSession

COOKIE_NAME = "opsbridge_session"
CSRF_HEADER = "x-csrf-token"
# Refresh idle expiry at most this often to avoid a write on every request.
_TOUCH_INTERVAL = timedelta(seconds=60)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class SessionInfo:
    session_hash: str
    user_id: uuid.UUID
    username: str
    display_name: str
    csrf_token: str
    expires_at: datetime
    idle_expires_at: datetime
    id_token: str | None

    def csrf_matches(self, presented: str | None) -> bool:
        return presented is not None and hmac.compare_digest(presented, self.csrf_token)


def create_session(
    db: Database, settings: Settings, user_id: uuid.UUID, id_token: str | None
) -> str:
    """Create a session and return the raw cookie token (never stored)."""
    token = secrets.token_urlsafe(32)
    h = hash_token(token)
    now = datetime.now(UTC)
    with db.session(DbContext(session_hash=h)) as s:
        s.add(
            UserSession(
                id=h,
                user_id=user_id,
                csrf_token=secrets.token_urlsafe(32),
                id_token=id_token,
                last_seen_at=now,
                expires_at=now + timedelta(hours=settings.session_absolute_hours),
                idle_expires_at=now + timedelta(minutes=settings.session_idle_minutes),
            )
        )
    return token


def load_session(db: Database, settings: Settings, token: str | None) -> SessionInfo | None:
    """Return the active session for a cookie token, or None if absent/expired/revoked."""
    if not token or len(token) > 200:
        return None
    h = hash_token(token)
    now = datetime.now(UTC)
    with db.session(DbContext(session_hash=h)) as s:
        row = s.get(UserSession, h)
        if row is None or row.revoked_at is not None:
            return None
        if row.expires_at <= now or row.idle_expires_at <= now:
            return None
        if now - row.last_seen_at >= _TOUCH_INTERVAL:
            row.last_seen_at = now
            row.idle_expires_at = min(
                row.expires_at, now + timedelta(minutes=settings.session_idle_minutes)
            )
        user_id = row.user_id
        info = (row.csrf_token, row.expires_at, row.idle_expires_at, row.id_token)
    # The user row is visible to the user's own context.
    with db.session(DbContext(user_id=user_id)) as s:
        user = s.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if user is None:
            return None
        return SessionInfo(
            session_hash=h,
            user_id=user_id,
            username=user.username,
            display_name=user.display_name,
            csrf_token=info[0],
            expires_at=info[1],
            idle_expires_at=info[2],
            id_token=info[3],
        )


def prune_sessions(db: Database, keep: timedelta) -> int:
    """Delete sessions that ended (expired, idle or revoked) more than `keep` ago, with the
    ID tokens they hold; returns how many. Runs as the worker through `ops.prune_sessions`
    (migration 0021): sessions are otherwise visible only to their own holder."""
    with db.session(DbContext()) as s:
        n = s.execute(text("SELECT ops.prune_sessions(:keep)"), {"keep": keep}).scalar_one()
    return int(n)


def revoke_session(db: Database, session_hash: str) -> None:
    with db.session(DbContext(session_hash=session_hash)) as s:
        row = s.get(UserSession, session_hash)
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)


def upsert_login_user(
    db: Database,
    *,
    issuer: str,
    subject: str,
    username: str,
    display_name: str,
    email: str | None,
    email_verified: bool = False,
) -> uuid.UUID:
    """Find or create the user for a verified OIDC identity. Grants no tenant access."""
    ctx = DbContext(oidc_issuer=issuer, oidc_subject=subject)
    now = datetime.now(UTC)
    with db.session(ctx) as s:
        user = s.execute(
            select(User).where(User.issuer == issuer, User.subject == subject)
        ).scalar_one_or_none()
        if user is None:
            user = User(
                issuer=issuer,
                subject=subject,
                username=username,
                display_name=display_name,
                email=email,
                email_verified=bool(email and email_verified),
                last_login_at=now,
            )
            s.add(user)
            s.flush()
        else:
            user.username = username
            user.display_name = display_name
            user.email = email
            user.email_verified = bool(email and email_verified)
            user.last_login_at = now
        return user.id
