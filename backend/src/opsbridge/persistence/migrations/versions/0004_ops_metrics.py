"""Aggregate-only operational metrics across tenants without weakening RLS.

`ops.metrics_snapshot()` is SECURITY DEFINER and owned by the NOLOGIN role
`opsbridge_ops_reader` (created by infra/compose/postgres/20-opsbridge-databases.sh).
That role may read only status/timestamp columns of three tables, through RLS policies
that apply to it alone, and the function returns counts and ages — never rows, ids or
tenant identifiers. The worker and API roles may execute it; nothing else is granted.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER = "opsbridge_ops_reader"
COLUMNS = {
    "proposals": "status, status_changed_at",
    "operations": "status, review_required, created_at",
    "workflow_runs": "status, created_at, finished_at",
}


def upgrade() -> None:
    exists = (
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": READER})
        .scalar()
    )
    if not exists:
        raise RuntimeError(
            f"role {READER} is missing; run `make infra-up` "
            "(infra/compose/postgres/20-opsbridge-databases.sh creates it)"
        )
    op.execute("CREATE SCHEMA ops")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA ops TO {READER}")
    op.execute("GRANT USAGE ON SCHEMA ops TO opsbridge_app, opsbridge_worker")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {READER}")
    for table, cols in COLUMNS.items():
        op.execute(f"GRANT SELECT ({cols}) ON {table} TO {READER}")
        op.execute(
            f"CREATE POLICY ops_reader_aggregate ON {table} FOR SELECT TO {READER} USING (true)"
        )
    op.execute(
        """
        CREATE FUNCTION ops.metrics_snapshot()
        RETURNS TABLE (metric text, value double precision)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT 'approval_backlog', count(*)::double precision
            FROM proposals WHERE status = 'awaiting_approval'
          UNION ALL
          SELECT 'approval_backlog_oldest_age_seconds',
                 coalesce(extract(epoch FROM now() - min(status_changed_at)), 0)::double precision
            FROM proposals WHERE status = 'awaiting_approval'
          UNION ALL
          SELECT 'operations_in_flight', count(*)::double precision
            FROM operations WHERE status IN ('pending_dispatch', 'executing')
          UNION ALL
          SELECT 'operations_outcome_unknown', count(*)::double precision
            FROM operations WHERE status = 'outcome_unknown'
          UNION ALL
          SELECT 'operations_review_required', count(*)::double precision
            FROM operations WHERE review_required
          UNION ALL
          SELECT 'runs_active', count(*)::double precision
            FROM workflow_runs WHERE status IN ('queued', 'running')
          UNION ALL
          SELECT 'runs_failed_last_hour', count(*)::double precision
            FROM workflow_runs WHERE status = 'failed' AND finished_at > now() - interval '1 hour'
        $$
        """
    )
    op.execute(f"ALTER FUNCTION ops.metrics_snapshot() OWNER TO {READER}")
    op.execute("REVOKE ALL ON FUNCTION ops.metrics_snapshot() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION ops.metrics_snapshot() TO opsbridge_app, opsbridge_worker"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops.metrics_snapshot()")
    op.execute("DROP SCHEMA IF EXISTS ops")
    for table in COLUMNS:
        op.execute(f"DROP POLICY IF EXISTS ops_reader_aggregate ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM {READER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {READER}")
