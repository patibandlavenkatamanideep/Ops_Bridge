"""Record when the purchasing source no longer holds a document.

A receipt, allocation or supplier message deleted, voided or moved to another PO in the
source system stayed current in OpsBridge and kept counting in matching. An investigation now
marks such documents withdrawn (from the source's tombstones, a 404, or absence from a
complete listing for the PO) and supersedes their records; a document that returns is current
again as a new version. Existing grants (table-level UPDATE for the app and worker roles)
cover the column.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_documents",
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source_documents", "withdrawn_at")
