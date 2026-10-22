"""At most one applied local action per operation.

OpsBridge-internal actions (review notes, supplier drafts, case assignment) now commit their
effect, a succeeded attempt and the operation's confirmation in one transaction. This partial
unique index lets a given operation have only one such attempt, so a redelivered or retried
dispatch cannot apply the action twice. Creating it fails if a database already holds a
duplicate from the earlier, non-atomic code; review those operations before upgrading.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_attempts_one_local_effect",
        "attempts",
        ["tenant_id", "operation_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'dispatch' AND status = 'succeeded' AND connector_version = 'opsbridge-local/1'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_attempts_one_local_effect", table_name="attempts")
