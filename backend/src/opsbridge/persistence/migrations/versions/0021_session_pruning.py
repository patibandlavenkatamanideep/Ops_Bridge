"""Remove ended sessions, and the ID tokens they hold.

A session that expired, went idle or was revoked stayed in `user_sessions` for good, with the
identity provider's ID token (name, e-mail and other claims) in it (production review A16).
`ops.prune_sessions(keep)` deletes sessions that ended more than `keep` ago and returns how
many; the worker calls it on its schedule. It is SECURITY DEFINER, owned by the NOLOGIN role
`opsbridge_session_pruner`, which may only read and delete sessions; the worker may execute
it and gains nothing else. The role is created by infra/compose/postgres/20-opsbridge-databases.sh
(the migrator cannot create roles); the upgrade stops with that instruction if it is missing.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRUNER = "opsbridge_session_pruner"


def upgrade() -> None:
    exists = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": PRUNER})
        .first()
    )
    if exists is None:
        raise RuntimeError(
            f"0021 needs the role {PRUNER}: run infra/compose/postgres/20-opsbridge-databases.sh "
            "as the database superuser (make infra-up does) or create it as that script does"
        )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {PRUNER}")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA ops TO {PRUNER}")
    op.execute(f"GRANT SELECT, DELETE ON user_sessions TO {PRUNER}")
    op.execute(f"CREATE POLICY session_prune ON user_sessions FOR ALL TO {PRUNER} USING (true)")
    op.execute(
        """
        CREATE FUNCTION ops.prune_sessions(keep interval)
        RETURNS integer
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH gone AS (
            DELETE FROM user_sessions
            WHERE least(coalesce(revoked_at, 'infinity'::timestamptz), expires_at, idle_expires_at)
                  < now() - keep
            RETURNING 1
          )
          SELECT count(*)::integer FROM gone
        $$
        """
    )
    op.execute(f"ALTER FUNCTION ops.prune_sessions(interval) OWNER TO {PRUNER}")
    op.execute("REVOKE ALL ON FUNCTION ops.prune_sessions(interval) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION ops.prune_sessions(interval) TO opsbridge_worker")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops.prune_sessions(interval)")
    op.execute("DROP POLICY IF EXISTS session_prune ON user_sessions")
    op.execute(f"REVOKE SELECT, DELETE ON user_sessions FROM {PRUNER}")
    op.execute(f"REVOKE USAGE, CREATE ON SCHEMA ops FROM {PRUNER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {PRUNER}")
