"""Record the request deadline a create was sent with.

After a create whose response was lost, a lookup that finds nothing used to count as proof
that the create had not happened, and the task was sent again. A create still in flight at
the provider (or behind the casework MCP hop) could land after that lookup, so a provider
without idempotency keys got the task twice. A create now carries a deadline after which the
provider must not commit it; absence is proof only after the deadline (plus clock allowance)
and only for providers that honour deadlines. Existing grants (table-level INSERT/UPDATE for
the worker) cover the column.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("attempts", "deadline_at")
