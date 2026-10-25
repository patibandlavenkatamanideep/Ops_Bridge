"""Bind each proposal's destination and outgoing text into the approved payload.

`destination` names where the action goes (connector plus organization or repository; never
credentials) and `outbound` the exact title and body that leave OpsBridge for an external
action. Both enter the canonical payload digest, so approval covers them, and both are
recomputed when approving and before every dispatch. Proposals made before this revision
keep NULL (and their digests); an external one of those can no longer be approved or
dispatched and must be proposed again. Existing grants on `proposals` cover the columns.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("proposals", sa.Column("destination", postgresql.JSONB(), nullable=True))
    op.add_column("proposals", sa.Column("outbound", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("proposals", "outbound")
    op.drop_column("proposals", "destination")
