"""Record the status a field had before each correction (review N05).

A correction sets the field's status to `corrected` and kept no record of what it was, so a
pilot report of a cut-off could not say what a field corrected after it had been then: the
report changed after the cut-off. `previous_status` is the field's status just before the
correction. Corrections made before this upgrade have none, and a report counts their fields
as of unrecorded status rather than guess. Additive and nullable: the upgrade rewrites no row,
and the downgrade only drops the column.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("field_corrections", sa.Column("previous_status", sa.String(24), nullable=True))


def downgrade() -> None:
    op.drop_column("field_corrections", "previous_status")
