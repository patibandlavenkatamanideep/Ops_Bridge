"""Queue artifact bytes for deletion instead of deleting them inside the retention sweep.

Artifacts of a tenant share the bytes of identical content. The sweep deleted those bytes as
soon as one row reached its cutoff (a newer copy then lost its content) and before the
database committed. Now the sweep marks rows deleted and queues their keys here in one
transaction; a purge deletes the bytes only when no live row references the key, under a
per-content advisory lock that writers also take. Row-level security is forced; only the
worker role reads and writes the queue.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blob_deletions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(length=300), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_blob_deletions_tenant_id_id"),
    )
    op.create_index("ix_blob_deletions_tenant_id", "blob_deletions", ["tenant_id"])
    op.create_index(
        "uq_blob_deletions_pending",
        "blob_deletions",
        ["tenant_id", "storage_key"],
        unique=True,
        postgresql_where=sa.text("done_at IS NULL"),
    )
    op.execute(
        """
        ALTER TABLE blob_deletions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE blob_deletions FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON blob_deletions
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
        GRANT SELECT, INSERT, UPDATE ON blob_deletions TO opsbridge_worker;
        """
    )


def downgrade() -> None:
    op.drop_table("blob_deletions")
