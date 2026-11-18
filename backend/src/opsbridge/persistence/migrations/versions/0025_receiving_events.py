"""Receiving and disposition events, and the reason a receipt line was rejected.

A receipt line could say that units were accepted, rejected or reversed, but not why they
were rejected, and nothing could record what happened after the receipt was posted: a
rejection learned later, a return, a replacement, a promised or posted credit. Such events
arrive as their own source documents (record type `receiving_event`) and are stored in
`receiving_events`, one row per event, tied to the document version it was read from like
every other source record. A newer version supersedes the older rows; both stay.

`occurred` is the time the source states for the event, as given (a date, a time with a zone,
or NULL when the source does not say); `created_at` is when OpsBridge recorded it, so a
rejection dated before an approval can be shown as learned after it. Existing receipt lines
get a NULL reason: nothing is inferred for them. Row-level security is forced; the app and
worker roles insert and supersede rows in a tenant's context, every role may read them.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'purchase_order','invoice','goods_receipt','supplier_message','policy',"
    "'allocation_history','attachment'"
)


def upgrade() -> None:
    op.add_column("receipt_lines", sa.Column("reason", sa.Text(), nullable=True))
    op.drop_constraint("ck_source_documents_kind", "source_documents", type_="check")
    op.create_check_constraint(
        "ck_source_documents_kind", "source_documents", f"kind in ({_KINDS},'receiving_event')"
    )
    op.create_table(
        "receiving_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("po_number", sa.String(length=100), nullable=False),
        sa.Column("po_line_number", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("occurred", sa.String(length=40), nullable=True),
        sa.Column("receipt_number", sa.String(length=100), nullable=True),
        sa.Column("follows", sa.String(length=100), nullable=True),
        sa.Column("invoice_number", sa.String(length=100), nullable=True),
        sa.Column("document_number", sa.String(length=100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "event_type in ('rejection','return','replacement_promised','replacement_delivery',"
            "'replacement_acceptance','credit_promised','credit_posted')",
            name="ck_receiving_events_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name="fk_receiving_events_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_receiving_events_tenant_id_id"),
    )
    op.create_index("ix_receiving_events_tenant_id", "receiving_events", ["tenant_id"])
    op.create_index("ix_receiving_events_po", "receiving_events", ["tenant_id", "po_number"])
    op.create_index(
        "uq_receiving_events_current",
        "receiving_events",
        ["tenant_id", "event_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.execute(
        """
        ALTER TABLE receiving_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE receiving_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON receiving_events
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
        GRANT SELECT ON receiving_events TO opsbridge_app, opsbridge_worker, opsbridge_mcp;
        GRANT INSERT, UPDATE ON receiving_events TO opsbridge_app, opsbridge_worker;
        """
    )


def downgrade() -> None:
    """Drops the events and the reasons. The documents they were read from stay (they are
    history) but are marked withdrawn, and their receipt records superseded, so nothing is
    concluded from a record that lost part of what it said; the next read of each, if the
    source still holds it, stores it again as a new version. The old kind constraint returns
    NOT VALID, so it binds new rows without rejecting the existing receiving-event documents.
    """
    conn = op.get_bind()
    for (tenant_id,) in conn.execute(sa.text("SELECT id FROM tenants")).all():
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            sa.text(
                "UPDATE goods_receipts SET superseded_at = now() WHERE superseded_at IS NULL "
                "AND id IN (SELECT goods_receipt_id FROM receipt_lines WHERE reason IS NOT NULL)"
            )
        )
        conn.execute(
            sa.text(
                "UPDATE source_documents d SET withdrawn_at = now() WHERE withdrawn_at IS NULL "
                "AND (kind = 'receiving_event' OR id IN ("
                "  SELECT v.document_id FROM document_versions v"
                "  JOIN goods_receipts g ON g.source_version_id = v.id"
                "  JOIN receipt_lines l ON l.goods_receipt_id = g.id"
                "  WHERE l.reason IS NOT NULL))"
            )
        )
    conn.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    op.drop_table("receiving_events")
    op.drop_constraint("ck_source_documents_kind", "source_documents", type_="check")
    op.execute(
        f"ALTER TABLE source_documents ADD CONSTRAINT ck_source_documents_kind "
        f"CHECK (kind in ({_KINDS})) NOT VALID"
    )
    op.drop_column("receipt_lines", "reason")
