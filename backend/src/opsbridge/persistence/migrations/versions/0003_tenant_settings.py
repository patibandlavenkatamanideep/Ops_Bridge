"""Per-tenant settings with RLS.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("model_mode", sa.String(24), nullable=False, server_default="mock"),
        sa.Column("task_connector", sa.String(32), nullable=False, server_default="sim_tasks"),
        sa.Column(
            "retention", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "model_mode in ('rules_only','mock','anthropic','ollama')",
            name="ck_tenant_settings_mode",
        ),
    )
    op.execute("ALTER TABLE tenant_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_settings USING (tenant_id = app_current_tenant()) "
        "WITH CHECK (tenant_id = app_current_tenant())"
    )
    op.execute("GRANT SELECT ON tenant_settings TO opsbridge_app, opsbridge_worker, opsbridge_mcp")
    op.execute("GRANT INSERT, UPDATE ON tenant_settings TO opsbridge_app")


def downgrade() -> None:
    op.drop_table("tenant_settings")
