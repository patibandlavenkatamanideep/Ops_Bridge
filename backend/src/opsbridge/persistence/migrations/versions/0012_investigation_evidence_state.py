"""Record the evidence state each investigation was computed from.

`evidence_state` holds the invoice and PO an investigation matched on and a digest of the
evidence before matching read it (current versions, the case's linked documents, and
corrections on them; services/freshness.py). Proposing, approving, dispatching and resolving
recompute it and refuse a result whose evidence changed since. Investigations recorded before
this revision have NULL and count as stale: re-run them. Existing grants cover the column.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigation_versions",
        sa.Column("evidence_state", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investigation_versions", "evidence_state")
