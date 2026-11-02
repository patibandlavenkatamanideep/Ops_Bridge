"""End sessions the identity provider says have ended (OpenID Connect Back-Channel Logout).

A person signed out at the identity provider kept OpsBridge until the session went idle or
reached its absolute limit (production review A16). Each session now records the identity
provider's session id (`sid`) from its ID token, and `ops.revoke_idp_sessions(user, sid)`
revokes the live sessions a logout token names and returns how many:
- a person and a session: that person's sessions from it, and their sessions recorded
  without one (opened before this upgrade), since those cannot be told apart;
- a person only: all of their sessions;
- a session only: the sessions opened from it.

Sessions are visible only to their own cookie hash, and a logout token names no cookie, so the
function is SECURITY DEFINER, owned by the NOLOGIN role `opsbridge_session_revoker`, which may
read only who a session belongs to and when it ends, and set only `revoked_at`. The API may
execute it and gains nothing else. The role is created by
infra/compose/postgres/20-opsbridge-databases.sh (the migrator cannot create roles); the
upgrade stops with that instruction if it is missing.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVOKER = "opsbridge_session_revoker"
READS = "user_id, idp_session_id, revoked_at, expires_at, idle_expires_at"


def upgrade() -> None:
    exists = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": REVOKER})
        .first()
    )
    if exists is None:
        raise RuntimeError(
            f"0022 needs the role {REVOKER}: run infra/compose/postgres/20-opsbridge-databases.sh "
            "as the database superuser (make infra-up does) or create it as that script does"
        )
    op.add_column("user_sessions", sa.Column("idp_session_id", sa.String(255), nullable=True))
    op.create_index("ix_user_sessions_idp_session_id", "user_sessions", ["idp_session_id"])
    op.execute(f"GRANT USAGE ON SCHEMA public TO {REVOKER}")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA ops TO {REVOKER}")
    op.execute(f"GRANT SELECT ({READS}), UPDATE (revoked_at) ON user_sessions TO {REVOKER}")
    op.execute(
        f"CREATE POLICY session_revoke ON user_sessions FOR ALL TO {REVOKER} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(
        """
        CREATE FUNCTION ops.revoke_idp_sessions(p_user uuid, p_sid text)
        RETURNS integer
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH ended AS (
            UPDATE user_sessions SET revoked_at = now()
            WHERE revoked_at IS NULL
              AND expires_at > now() AND idle_expires_at > now()
              AND (p_user IS NOT NULL OR p_sid IS NOT NULL)
              AND (p_user IS NULL OR user_id = p_user)
              AND (p_sid IS NULL OR idp_session_id = p_sid
                   OR (p_user IS NOT NULL AND idp_session_id IS NULL))
            RETURNING 1
          )
          SELECT count(*)::integer FROM ended
        $$
        """
    )
    op.execute(f"ALTER FUNCTION ops.revoke_idp_sessions(uuid, text) OWNER TO {REVOKER}")
    op.execute("REVOKE ALL ON FUNCTION ops.revoke_idp_sessions(uuid, text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION ops.revoke_idp_sessions(uuid, text) TO opsbridge_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops.revoke_idp_sessions(uuid, text)")
    op.execute("DROP POLICY IF EXISTS session_revoke ON user_sessions")
    op.execute(f"REVOKE ALL ON user_sessions FROM {REVOKER}")
    op.execute(f"REVOKE USAGE, CREATE ON SCHEMA ops FROM {REVOKER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {REVOKER}")
    op.drop_index("ix_user_sessions_idp_session_id", table_name="user_sessions")
    op.drop_column("user_sessions", "idp_session_id")
