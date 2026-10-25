"""Versioned, tenant-bound task connections.

The follow-up task destination came from deployment-wide settings (the GitHub repository and
token) plus the tenant's connector choice, so every tenant on GitHub wrote to one repository,
and a changed setting changed where recovery looked for a write that had already landed. Each
tenant now has a versioned connection: connector, non-secret destination and a reference to a
credential in the deployment's secret store (never the secret). One version is active; a
change supersedes it, revoking stops new writes. Row-level security is forced; the app role
reads and writes a tenant's rows in its context, the worker reads them.

Every tenant gets version 1 from its current connector. A GitHub tenant's repository was a
deployment setting this migration cannot read, so it starts empty: such a tenant must set its
repository before follow-up tasks can be proposed or sent.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-25
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("connector", sa.String(length=32), nullable=False),
        sa.Column("destination", postgresql.JSONB(), nullable=False),
        sa.Column("credential_ref", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('active','superseded','revoked')", name="ck_task_connections_status"
        ),
        sa.CheckConstraint(
            "connector in ('sim_tasks','github_issues')", name="ck_task_connections_connector"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["ended_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_task_connections_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_task_connections_version"),
    )
    op.create_index("ix_task_connections_tenant_id", "task_connections", ["tenant_id"])
    op.create_index(
        "uq_task_connections_active",
        "task_connections",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.execute(
        """
        ALTER TABLE task_connections ENABLE ROW LEVEL SECURITY;
        ALTER TABLE task_connections FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON task_connections
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
        GRANT SELECT, INSERT, UPDATE ON task_connections TO opsbridge_app;
        GRANT SELECT ON task_connections TO opsbridge_worker, opsbridge_mcp;
        """
    )
    # Version 1 per tenant, written in each tenant's context (tenant_settings is under
    # row-level security for the migrator too).
    conn = op.get_bind()
    for tenant_id, slug in conn.execute(sa.text("SELECT id, slug FROM tenants")).all():
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        name = (
            conn.execute(
                sa.text("SELECT task_connector FROM tenant_settings WHERE tenant_id = :t"),
                {"t": tenant_id},
            ).scalar()
            or "sim_tasks"
        )
        destination = (
            {"connector": "github_issues", "repository": ""}
            if name == "github_issues"
            else {"connector": "sim_tasks", "organization": slug}
        )
        conn.execute(
            sa.text(
                "INSERT INTO task_connections (id, tenant_id, version, connector, destination, "
                "status, reason) VALUES (:i, :t, 1, :c, CAST(:d AS jsonb), 'active', :r)"
            ),
            {
                "i": str(uuid.uuid4()),
                "t": tenant_id,
                "c": name,
                "d": json.dumps(destination),
                "r": "created from the tenant's connector setting (migration 0017)",
            },
        )
    conn.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    op.drop_table("task_connections")
