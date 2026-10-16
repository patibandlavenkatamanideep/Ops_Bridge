"""Tenants due for scheduled retention, without weakening RLS.

`ops.tenants_with_retention()` (SECURITY DEFINER, owned by NOLOGIN `opsbridge_ops_reader`)
returns only the ids of tenants that configured retention, so the worker can schedule one
sweep per tenant per day. The reader role may see only `tenant_settings.tenant_id` and
`retention`; the worker may execute the function and nothing else is granted.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER = "opsbridge_ops_reader"


def upgrade() -> None:
    op.execute(f"GRANT SELECT (tenant_id, retention) ON tenant_settings TO {READER}")
    op.execute(
        f"CREATE POLICY ops_reader_retention ON tenant_settings FOR SELECT TO {READER} USING (true)"
    )
    op.execute(
        """
        CREATE FUNCTION ops.tenants_with_retention()
        RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT tenant_id FROM tenant_settings
          WHERE retention IS NOT NULL AND retention <> '{}'::jsonb
        $$
        """
    )
    op.execute(f"ALTER FUNCTION ops.tenants_with_retention() OWNER TO {READER}")
    op.execute("REVOKE ALL ON FUNCTION ops.tenants_with_retention() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION ops.tenants_with_retention() TO opsbridge_worker")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops.tenants_with_retention()")
    op.execute("DROP POLICY IF EXISTS ops_reader_retention ON tenant_settings")
    op.execute("REVOKE SELECT (tenant_id, retention) ON tenant_settings FROM opsbridge_ops_reader")
