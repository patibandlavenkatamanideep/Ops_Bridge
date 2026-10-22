"""Retention for invitation email addresses.

A tenant's retention sweep (worker role) can clear the invited address on invitations that
were accepted, withdrawn or expired longer ago than `invitations_days`. The row stays, with
its roles, status, timestamps and token hash, plus when and why the address was cleared. The
worker sees invitations only in the tenant's context and may update only these three columns.

The downgrade fails if any address was cleared: the earlier schema requires one.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("membership_invitations", "email", nullable=True)
    op.add_column(
        "membership_invitations",
        sa.Column("email_cleared_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("membership_invitations", sa.Column("deletion_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_membership_invitations_email_cleared",
        "membership_invitations",
        "(email IS NULL) = (email_cleared_at IS NOT NULL)",
    )
    op.execute(
        """
        CREATE POLICY invitation_worker ON membership_invitations TO opsbridge_worker
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
        GRANT SELECT ON membership_invitations TO opsbridge_worker;
        GRANT UPDATE (email, email_cleared_at, deletion_reason)
          ON membership_invitations TO opsbridge_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON membership_invitations FROM opsbridge_worker;
        DROP POLICY invitation_worker ON membership_invitations;
        """
    )
    op.drop_constraint(
        "ck_membership_invitations_email_cleared", "membership_invitations", type_="check"
    )
    op.drop_column("membership_invitations", "deletion_reason")
    op.drop_column("membership_invitations", "email_cleared_at")
    op.alter_column("membership_invitations", "email", nullable=False)
