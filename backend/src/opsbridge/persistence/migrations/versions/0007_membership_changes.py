"""Record who last changed a membership in the app, and why.

Tenant administrators can now change roles and approval scope and revoke or reinstate members.
The free-text reason is kept on the membership row rather than in the append-only audit
trail, whose details stay structural (before/after roles, scope and status). Existing grants
and row-level security on `memberships` already cover these columns.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memberships", sa.Column("updated_by", postgresql.UUID(as_uuid=True)))
    op.add_column("memberships", sa.Column("change_reason", sa.String(length=500)))
    op.create_foreign_key(
        "fk_memberships_updated_by", "memberships", "users", ["updated_by"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_memberships_updated_by", "memberships", type_="foreignkey")
    op.drop_column("memberships", "change_reason")
    op.drop_column("memberships", "updated_by")
