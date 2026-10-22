"""Shared API rate-limit counters across API instances.

One row per (key, fixed window); the API role counts with a single upsert and prunes expired
windows. RLS is enabled and forced like on every table; only the API role may use the table.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_windows",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limit_windows")),
    )
    op.execute("ALTER TABLE rate_limit_windows ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE rate_limit_windows FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY rate_limit_app ON rate_limit_windows TO opsbridge_app "
        "USING (true) WITH CHECK (true)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limit_windows TO opsbridge_app")


def downgrade() -> None:
    op.drop_table("rate_limit_windows")
