"""Membership invitations, and whether a user's email was verified by the identity provider.

An administrator invites an email address with roles; the person accepts with a single-use
token after signing in, and only if the identity provider verified that address. Row-level
security is enabled and forced: the API role sees a tenant's invitations in that tenant's
context, and outside any tenant only the one invitation whose token hash is in the
transaction context (to find the tenant before accepting).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "membership_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("roles", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("approval_scope", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('pending','accepted','revoked')", name="ck_membership_invitations_status"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_membership_invitations_tenant_id_id"),
        sa.UniqueConstraint("token_hash", name="uq_membership_invitations_token_hash"),
    )
    op.create_index("ix_membership_invitations_tenant_id", "membership_invitations", ["tenant_id"])
    op.execute(
        """
        CREATE FUNCTION app_invite_token_hash() RETURNS text LANGUAGE sql STABLE AS
          $$ SELECT NULLIF(current_setting('app.invite_token_hash', true), '') $$;
        ALTER TABLE membership_invitations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE membership_invitations FORCE ROW LEVEL SECURITY;
        CREATE POLICY invitation_tenant ON membership_invitations TO opsbridge_app
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
        CREATE POLICY invitation_by_token ON membership_invitations FOR SELECT TO opsbridge_app
          USING (token_hash = app_invite_token_hash());
        GRANT SELECT, INSERT, UPDATE ON membership_invitations TO opsbridge_app;
        """
    )


def downgrade() -> None:
    op.drop_table("membership_invitations")
    op.execute("DROP FUNCTION IF EXISTS app_invite_token_hash()")
    op.drop_column("users", "email_verified")
