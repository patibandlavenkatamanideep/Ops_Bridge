"""Scope invoice identity by supplier.

Two suppliers may issue the same invoice number. The current-record index was
(tenant, invoice_number), so one supplier's invoice superseded the other's and a case could be
matched against the wrong supplier's invoice. It is now (tenant, supplier, invoice_number).
Every database that satisfied the old index satisfies the new one, so no data changes.

`cases.supplier_id` records the supplier when the requester names it; existing cases have
NULL and match by number alone, which now stops for review if two suppliers' invoices carry
that number. Existing grants on `cases` cover the column.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("supplier_id", sa.String(length=100), nullable=True))
    op.drop_index("uq_invoices_current", table_name="invoices")
    op.create_index(
        "uq_invoices_current",
        "invoices",
        ["tenant_id", sa.text("coalesce(supplier_key, '')"), "invoice_number"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    # Fails if two suppliers' invoices with the same number are both current.
    op.drop_index("uq_invoices_current", table_name="invoices")
    op.create_index(
        "uq_invoices_current",
        "invoices",
        ["tenant_id", "invoice_number"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.drop_column("cases", "supplier_id")
