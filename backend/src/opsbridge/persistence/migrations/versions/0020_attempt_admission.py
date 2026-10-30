"""Record when the casework MCP tool admitted a create.

The casework tool accepted any create for an executing operation whose delegation was valid,
so a replayed or retransmitted request wrote a second task outside the executor's attempt
ledger, and the tool ran weaker checks than the executor (production review A05). It now
admits a create with the executor's own admission function, once per attempt the executor
opened: setting `admitted_at` where it is still NULL is the single use. The MCP role may
update that column and no other.

A person's review of an unknown outcome now completes the operation as the executor does,
including marking its outbox event delivered, so the API role may set `delivered_at`.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("GRANT UPDATE (admitted_at) ON attempts TO opsbridge_mcp")
    op.execute("GRANT UPDATE (delivered_at) ON outbox_events TO opsbridge_app")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (delivered_at) ON outbox_events FROM opsbridge_app")
    op.execute("REVOKE UPDATE (admitted_at) ON attempts FROM opsbridge_mcp")
    op.drop_column("attempts", "admitted_at")
