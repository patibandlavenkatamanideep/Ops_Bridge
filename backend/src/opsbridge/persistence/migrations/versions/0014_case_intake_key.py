"""Record the idempotency key and request that created each case.

A replayed intake request must carry the same Idempotency-Key and the same request; the key
alone used to return whichever case matched the invoice, even for a different request or a
different key. Cases created before this revision have NULL and are not replayable by key;
their invoices still refuse duplicates. Existing grants on `cases` cover the columns.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("intake_key", sa.String(length=100), nullable=True))
    op.add_column("cases", sa.Column("intake_digest", sa.String(length=71), nullable=True))
    op.create_index(
        "uq_cases_intake_key",
        "cases",
        ["tenant_id", "requester_id", "intake_key"],
        unique=True,
        postgresql_where=sa.text("intake_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_cases_intake_key", table_name="cases")
    op.drop_column("cases", "intake_digest")
    op.drop_column("cases", "intake_key")
