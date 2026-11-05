"""`ops.prune_sessions` refuses a period that would reach live sessions.

The worker's setting is checked in Python (above 0), but the function is what the worker may
execute, and it runs as the pruning role, which may delete any tenant's sessions. Given a
negative period, `now() - keep` lay in the future and every session ended "before" it:
`ops.prune_sessions('-100 years')` deleted live sessions too. The function now validates its
own argument, refusing a NULL or negative period (an interval that is negative overall, such
as '1 day -25 hours', included) with SQLSTATE 22023, and deletes nothing then. Its
signature, owner and grants are unchanged; the downgrade restores the 0021 body.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELETE = """
    DELETE FROM user_sessions
    WHERE least(coalesce(revoked_at, 'infinity'::timestamptz), expires_at, idle_expires_at)
          < now() - keep
"""


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION ops.prune_sessions(keep interval)
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          removed integer;
        BEGIN
          IF keep IS NULL OR keep < interval '0' THEN
            RAISE EXCEPTION 'ops.prune_sessions: keep must be a non-negative interval, got %', keep
              USING ERRCODE = '22023';
          END IF;
          WITH gone AS ({DELETE} RETURNING 1)
          SELECT count(*)::integer INTO removed FROM gone;
          RETURN removed;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION ops.prune_sessions(keep interval)
        RETURNS integer
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH gone AS ({DELETE} RETURNING 1)
          SELECT count(*)::integer FROM gone
        $$
        """
    )
