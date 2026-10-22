"""SQLAlchemy models.

Conventions:
- Server-assigned UUID primary keys; timezone-aware UTC timestamps.
- Every tenant-owned table has `tenant_id` and UNIQUE (tenant_id, id); child tables
  reference parents through composite (tenant_id, parent_id) foreign keys so a row
  cannot point at another tenant's data.
- Money and quantities are NUMERIC (exact) and surface as Decimal.
- Row-level security policies and grants are created in migrations, not here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
        Decimal: Numeric(),
        datetime: DateTime(timezone=True),
    }


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)


def _tenant_unique(table: str) -> UniqueConstraint:
    return UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id")


def _fk(
    cols: list[str], target: str, name: str, ondelete: str | None = None
) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", *cols],
        [f"{target}.tenant_id", f"{target}.id"],
        name=name,
        ondelete=ondelete,
    )


# ---------------------------------------------------------------------------------------
# Identity, tenancy, sessions
# ---------------------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Service tier is an entitlement (features/budgets), never an authorization grant.
    tier: Mapped[str] = mapped_column(String(32))
    pack_id: Mapped[str] = mapped_column(String(64))
    synthetic_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (CheckConstraint("tier in ('standard','enhanced')", name="ck_tenants_tier"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = pk()
    issuer: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(300))
    username: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    # Whether the identity provider vouched for `email` at the latest sign-in (OIDC
    # `email_verified`). Invitations are accepted only against a verified address.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = created()
    last_login_at: Mapped[datetime | None]
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    # Action types this member may approve when they hold the approver role.
    approval_scope: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    # Who last changed this membership in the app, and why (seeded rows have neither).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    change_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        _tenant_unique("memberships"),
        UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
        CheckConstraint("status in ('active','revoked')", name="ck_memberships_status"),
    )


class MembershipInvitation(Base):
    """A single-use invitation to join a tenant with given roles. Only the SHA-256 of the
    token is stored. It can be accepted once, before `expires_at` (database clock), by a
    signed-in user whose identity provider verified the invited email address. Retention
    clears the address once the invitation is closed or expired; the row stays."""

    __tablename__ = "membership_invitations"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    email: Mapped[str | None] = mapped_column(String(320))  # trimmed, lower case
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    approval_scope: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    token_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        _tenant_unique("membership_invitations"),
        UniqueConstraint("token_hash", name="uq_membership_invitations_token_hash"),
        CheckConstraint(
            "status in ('pending','accepted','revoked')", name="ck_membership_invitations_status"
        ),
        CheckConstraint(
            "(email IS NULL) = (email_cleared_at IS NOT NULL)",
            name="ck_membership_invitations_email_cleared",
        ),
    )


class RateLimitWindow(Base):
    """Shared API rate-limit counters (perimeter state, not tenant data): one row per key and
    fixed window. Keys are a session-cookie hash or a client address, never raw tokens."""

    __tablename__ = "rate_limit_windows"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


class UserSession(Base):
    __tablename__ = "user_sessions"
    # SHA-256 of the opaque cookie value; the raw value is never stored.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    id_token: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    idle_expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class ServiceIdentity(Base):
    __tablename__ = "service_identities"
    id: Mapped[uuid.UUID] = pk()
    client_id: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # worker | mcp_client | connector
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()


class TenantSettings(Base):
    """Per-tenant operational configuration (model mode, task connector, retention)."""

    __tablename__ = "tenant_settings"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    model_mode: Mapped[str] = mapped_column(String(24), default="mock")
    task_connector: Mapped[str] = mapped_column(String(32), default="sim_tasks")
    retention: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint(
            "model_mode in ('rules_only','mock','anthropic','ollama')",
            name="ck_tenant_settings_mode",
        ),
    )


class TenantCounter(Base):
    """Per-tenant monotonically increasing counters (e.g. case numbers)."""

    __tablename__ = "tenant_counters"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)


# ---------------------------------------------------------------------------------------
# Policy and organization pack versions
# ---------------------------------------------------------------------------------------


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    policy_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[date] = mapped_column(Date)
    document: Mapped[dict[str, Any]]
    sha256: Mapped[str] = mapped_column(String(71))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("policy_versions"),
        UniqueConstraint("tenant_id", "policy_id", "version", name="uq_policy_versions_key"),
    )


class PackVersion(Base):
    __tablename__ = "pack_versions"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    pack_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]]
    sha256: Mapped[str] = mapped_column(String(71))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("pack_versions"),
        UniqueConstraint("tenant_id", "pack_id", "version", name="uq_pack_versions_key"),
    )


# ---------------------------------------------------------------------------------------
# Evidence: source documents, versions, chunks, extracted fields, corrections, artifacts
# ---------------------------------------------------------------------------------------


class Artifact(Base):
    """Protected artifact manifest entry. Bytes live in artifact storage, not the DB."""

    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    kind: Mapped[str] = mapped_column(String(48))
    sha256: Mapped[str] = mapped_column(String(71))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(100))
    storage_key: Mapped[str | None] = mapped_column(String(300))
    classification: Mapped[str] = mapped_column(String(32), default="internal")
    derived_from_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    transformation: Mapped[dict[str, Any] | None]
    retention_until: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    deletion_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("artifacts"),
        _fk(["derived_from_id"], "artifacts", "fk_artifacts_derived_from"),
    )


class SourceDocument(Base):
    __tablename__ = "source_documents"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(32))  # source_api | upload | csv_import | pack
    title: Mapped[str] = mapped_column(String(300))
    classification: Mapped[str] = mapped_column(String(32), default="internal")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("source_documents"),
        UniqueConstraint("tenant_id", "source_key", name="uq_source_documents_key"),
        CheckConstraint(
            "kind in ('purchase_order','invoice','goods_receipt','supplier_message','policy',"
            "'allocation_history','attachment')",
            name="ck_source_documents_kind",
        ),
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_no: Mapped[int] = mapped_column(Integer)
    raw_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    raw_sha256: Mapped[str] = mapped_column(String(71))
    raw_size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(100))
    safe_filename: Mapped[str | None] = mapped_column(String(200))
    normalized_sha256: Mapped[str | None] = mapped_column(String(71))
    normalization_version: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    parser: Mapped[str | None] = mapped_column(String(64))
    parse_status: Mapped[str] = mapped_column(String(32))
    parse_detail: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("document_versions"),
        UniqueConstraint("tenant_id", "document_id", "version_no", name="uq_document_versions_no"),
        _fk(["document_id"], "source_documents", "fk_document_versions_document"),
        _fk(["raw_artifact_id"], "artifacts", "fk_document_versions_raw_artifact"),
        CheckConstraint(
            "parse_status in ('parsed','needs_review','unsupported','rejected')",
            name="ck_document_versions_parse_status",
        ),
    )


class EvidenceChunk(Base):
    __tablename__ = "evidence_chunks"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(String(120))
    page: Mapped[int | None] = mapped_column(Integer)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    text_content: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text_content)", persisted=True)
    )
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("evidence_chunks"),
        _fk(["version_id"], "document_versions", "fk_evidence_chunks_version"),
        Index("ix_evidence_chunks_tsv", "tsv", postgresql_using="gin"),
    )


class ExtractedField(Base):
    __tablename__ = "extracted_fields"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    record_type: Mapped[str] = mapped_column(String(48))
    record_key: Mapped[str] = mapped_column(String(200))
    field_path: Mapped[str] = mapped_column(String(200))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(24))
    locator: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(24))  # structured | parsed | pattern | ocr | model
    status: Mapped[str] = mapped_column(String(24))  # authoritative | needs_review | corrected
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("extracted_fields"),
        _fk(["version_id"], "document_versions", "fk_extracted_fields_version"),
        CheckConstraint(
            "status in ('authoritative','needs_review','corrected','rejected')",
            name="ck_extracted_fields_status",
        ),
    )


class FieldCorrection(Base):
    __tablename__ = "field_corrections"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    field_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("field_corrections"),
        _fk(["field_id"], "extracted_fields", "fk_field_corrections_field"),
    )


# ---------------------------------------------------------------------------------------
# Structured procurement records (derived from document versions)
# ---------------------------------------------------------------------------------------


class _RecordMixin:
    refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupplierRecord(Base):
    __tablename__ = "suppliers"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    supplier_key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("suppliers"),
        UniqueConstraint("tenant_id", "supplier_key", name="uq_suppliers_key"),
    )


class PurchaseOrderRecord(_RecordMixin, Base):
    __tablename__ = "purchase_orders"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    po_number: Mapped[str] = mapped_column(String(100))
    supplier_key: Mapped[str] = mapped_column(String(100))
    supplier_name: Mapped[str] = mapped_column(String(300))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16))
    __table_args__ = (
        _tenant_unique("purchase_orders"),
        _fk(["source_version_id"], "document_versions", "fk_purchase_orders_version"),
        Index(
            "uq_purchase_orders_current",
            "tenant_id",
            "po_number",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )


class POLineRecord(Base):
    __tablename__ = "po_lines"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    line_number: Mapped[str] = mapped_column(String(32))
    item_code: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal]
    unit: Mapped[str] = mapped_column(String(32))
    unit_price: Mapped[Decimal]
    discount_amount: Mapped[Decimal] = mapped_column(default=Decimal(0))
    refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        _tenant_unique("po_lines"),
        _fk(["purchase_order_id"], "purchase_orders", "fk_po_lines_po", ondelete="CASCADE"),
    )


class InvoiceRecord(_RecordMixin, Base):
    __tablename__ = "invoices"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    invoice_number: Mapped[str] = mapped_column(String(100))
    supplier_key: Mapped[str | None] = mapped_column(String(100))
    supplier_name: Mapped[str] = mapped_column(String(300))
    po_number: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    invoice_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    __table_args__ = (
        _tenant_unique("invoices"),
        _fk(["source_version_id"], "document_versions", "fk_invoices_version"),
        Index(
            "uq_invoices_current",
            "tenant_id",
            "invoice_number",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )


class InvoiceLineRecord(Base):
    __tablename__ = "invoice_lines"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    line_number: Mapped[str] = mapped_column(String(32))
    po_line_number: Mapped[str | None] = mapped_column(String(32))
    item_code: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal]
    unit: Mapped[str] = mapped_column(String(32))
    unit_price: Mapped[Decimal]
    discount_amount: Mapped[Decimal] = mapped_column(default=Decimal(0))
    kind: Mapped[str] = mapped_column(String(16), default="goods")
    refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        _tenant_unique("invoice_lines"),
        _fk(["invoice_id"], "invoices", "fk_invoice_lines_invoice", ondelete="CASCADE"),
    )


class GoodsReceiptRecord(_RecordMixin, Base):
    __tablename__ = "goods_receipts"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    receipt_number: Mapped[str] = mapped_column(String(100))
    po_number: Mapped[str] = mapped_column(String(100))
    received_date: Mapped[date] = mapped_column(Date)
    __table_args__ = (
        _tenant_unique("goods_receipts"),
        _fk(["source_version_id"], "document_versions", "fk_goods_receipts_version"),
        Index(
            "uq_goods_receipts_current",
            "tenant_id",
            "receipt_number",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )


class ReceiptLineRecord(Base):
    __tablename__ = "receipt_lines"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    po_line_number: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal]
    unit: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16), default="receipt")
    status: Mapped[str] = mapped_column(String(16), default="accepted")
    refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        _tenant_unique("receipt_lines"),
        _fk(["goods_receipt_id"], "goods_receipts", "fk_receipt_lines_receipt", ondelete="CASCADE"),
    )


class InvoiceAllocationRecord(_RecordMixin, Base):
    __tablename__ = "invoice_allocations"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    invoice_number: Mapped[str] = mapped_column(String(100))
    po_number: Mapped[str] = mapped_column(String(100))
    po_line_number: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(16))
    __table_args__ = (
        _tenant_unique("invoice_allocations"),
        _fk(["source_version_id"], "document_versions", "fk_invoice_allocations_version"),
    )


class SupplierAssertionRecord(_RecordMixin, Base):
    __tablename__ = "supplier_assertions"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    po_number: Mapped[str] = mapped_column(String(100))
    po_line_number: Mapped[str | None] = mapped_column(String(32))
    claim: Mapped[str] = mapped_column(String(48))
    quantity: Mapped[Decimal]
    unit: Mapped[str | None] = mapped_column(String(32))
    extraction_status: Mapped[str] = mapped_column(String(16))
    __table_args__ = (
        _tenant_unique("supplier_assertions"),
        _fk(["source_version_id"], "document_versions", "fk_supplier_assertions_version"),
    )


# ---------------------------------------------------------------------------------------
# Cases, conversations, workflow runs and investigation versions
# ---------------------------------------------------------------------------------------


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    case_number: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    invoice_number: Mapped[str | None] = mapped_column(String(100))
    po_number: Mapped[str | None] = mapped_column(String(100))
    # Stable key that prevents duplicate intake of the same blocked invoice.
    source_key: Mapped[str] = mapped_column(String(200))
    discrepancy_types: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    evidence_readiness: Mapped[str] = mapped_column(String(32), default="not_collected")
    status: Mapped[str] = mapped_column(String(32), default="new")
    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    assignee_group: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        _tenant_unique("cases"),
        UniqueConstraint("tenant_id", "case_number", name="uq_cases_number"),
        UniqueConstraint("tenant_id", "source_key", name="uq_cases_source_key"),
        CheckConstraint(
            "status in ('new','collecting_evidence','investigating','needs_information',"
            "'awaiting_review','follow_up_pending','resolved','closed_without_resolution')",
            name="ck_cases_status",
        ),
        Index("ix_cases_tenant_status_created", "tenant_id", "status", "created_at"),
    )


class CaseDocument(Base):
    __tablename__ = "case_documents"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    added_at: Mapped[datetime] = created()
    __table_args__ = (
        _fk(["case_id"], "cases", "fk_case_documents_case"),
        _fk(["document_id"], "source_documents", "fk_case_documents_document"),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(Integer)
    author_kind: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str | None] = mapped_column(Text)
    body_sha256: Mapped[str] = mapped_column(String(71))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created()
    deleted_at: Mapped[datetime | None]
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    deletion_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        _tenant_unique("conversation_messages"),
        UniqueConstraint("tenant_id", "case_id", "seq", name="uq_conversation_seq"),
        _fk(["case_id"], "cases", "fk_conversation_messages_case"),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    workflow_version: Mapped[str] = mapped_column(String(48))
    model_mode: Mapped[str] = mapped_column(String(24))
    response_mode: Mapped[str] = mapped_column(String(16), default="live")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    executed_by: Mapped[str | None] = mapped_column(String(200))
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    __table_args__ = (
        _tenant_unique("workflow_runs"),
        _fk(["case_id"], "cases", "fk_workflow_runs_case"),
        CheckConstraint(
            "status in ('queued','running','completed','failed','cancelled')",
            name="ck_workflow_runs_status",
        ),
    )


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(Integer)
    step_name: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16))
    input_digest: Mapped[str | None] = mapped_column(String(71))
    output: Mapped[dict[str, Any] | None]
    output_digest: Mapped[str | None] = mapped_column(String(71))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    __table_args__ = (
        _tenant_unique("workflow_steps"),
        UniqueConstraint("tenant_id", "run_id", "step_name", name="uq_workflow_steps_name"),
        _fk(["run_id"], "workflow_runs", "fk_workflow_steps_run"),
    )


class InvestigationVersion(Base):
    __tablename__ = "investigation_versions"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    outcome: Mapped[str] = mapped_column(String(32))
    findings: Mapped[list[Any]] = mapped_column(JSONB)
    explanation: Mapped[str] = mapped_column(Text)
    explanation_origin: Mapped[str] = mapped_column(String(32))
    recommendation: Mapped[dict[str, Any]]
    evidence_readiness: Mapped[str] = mapped_column(String(32))
    citations: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    uncertainty: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    policy_ref: Mapped[str] = mapped_column(String(100))
    rules_version: Mapped[str] = mapped_column(String(48))
    source_manifest: Mapped[list[Any]] = mapped_column(JSONB)
    source_manifest_digest: Mapped[str] = mapped_column(String(71))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("investigation_versions"),
        UniqueConstraint("tenant_id", "case_id", "version", name="uq_investigation_versions_no"),
        _fk(["case_id"], "cases", "fk_investigation_versions_case"),
        _fk(["run_id"], "workflow_runs", "fk_investigation_versions_run"),
        _fk(["policy_version_id"], "policy_versions", "fk_investigation_versions_policy"),
    )


class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    step_name: Mapped[str] = mapped_column(String(48))
    provider: Mapped[str] = mapped_column(String(32))
    requested_model: Mapped[str | None] = mapped_column(String(100))
    resolved_model: Mapped[str | None] = mapped_column(String(100))
    endpoint: Mapped[str | None] = mapped_column(String(200))
    sdk_version: Mapped[str | None] = mapped_column(String(48))
    prompt_template_id: Mapped[str] = mapped_column(String(100))
    prompt_template_digest: Mapped[str] = mapped_column(String(71))
    input_digest: Mapped[str] = mapped_column(String(71))
    output_digest: Mapped[str | None] = mapped_column(String(71))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_estimate_usd: Mapped[Decimal | None]
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16))  # live | recorded | mocked
    redaction: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        _tenant_unique("model_calls"),
        _fk(["run_id"], "workflow_runs", "fk_model_calls_run"),
    )


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    step_name: Mapped[str] = mapped_column(String(48))
    seq: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[str] = mapped_column(String(32))
    transport: Mapped[str] = mapped_column(String(16))
    input_digest: Mapped[str] = mapped_column(String(71))
    response_digest: Mapped[str | None] = mapped_column(String(71))
    outcome: Mapped[str] = mapped_column(String(16))  # ok | denied | error | timeout
    origin: Mapped[str] = mapped_column(String(16))
    request_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    response_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None]
    __table_args__ = (
        _tenant_unique("tool_invocations"),
        _fk(["run_id"], "workflow_runs", "fk_tool_invocations_run"),
    )


# ---------------------------------------------------------------------------------------
# Proposals, approvals, operations, attempts
# ---------------------------------------------------------------------------------------


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    proposal_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer)
    action_type: Mapped[str] = mapped_column(String(48))
    parameters: Mapped[dict[str, Any]]
    effect_summary: Mapped[str] = mapped_column(Text)
    affected_resources: Mapped[list[Any]] = mapped_column(JSONB)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB)
    source_manifest: Mapped[list[Any]] = mapped_column(JSONB)
    source_manifest_digest: Mapped[str] = mapped_column(String(71))
    investigation_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    policy_ref: Mapped[str] = mapped_column(String(100))
    required_permission: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(71))
    canonicalization: Mapped[str] = mapped_column(String(32))
    author_kind: Mapped[str] = mapped_column(String(16))  # user | agent | rules
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(24))
    generated_at: Mapped[datetime] = created()
    expires_at: Mapped[datetime]
    status_changed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    __table_args__ = (
        _tenant_unique("proposals"),
        UniqueConstraint("tenant_id", "proposal_key", "version", name="uq_proposals_key_version"),
        _fk(["case_id"], "cases", "fk_proposals_case"),
        _fk(["investigation_version_id"], "investigation_versions", "fk_proposals_investigation"),
        _fk(["policy_version_id"], "policy_versions", "fk_proposals_policy"),
        CheckConstraint(
            "status in ('draft','awaiting_approval','approved','rejected','expired','superseded')",
            name="ck_proposals_status",
        ),
    )


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    proposal_version: Mapped[int] = mapped_column(Integer)
    approver_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(16))
    payload_digest: Mapped[str] = mapped_column(String(71))
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_manifest_digest: Mapped[str] = mapped_column(String(71))
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    separation_of_duties: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = created()
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    __table_args__ = (
        _tenant_unique("approval_decisions"),
        _fk(["proposal_id"], "proposals", "fk_approval_decisions_proposal"),
        CheckConstraint("decision in ('approved','rejected')", name="ck_approval_decision"),
        # At most one approval can ever authorize a given proposal version.
        Index(
            "uq_approval_one_approved_per_proposal",
            "tenant_id",
            "proposal_id",
            unique=True,
            postgresql_where=text("decision = 'approved'"),
        ),
    )


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    operation_key: Mapped[str] = mapped_column(String(80))
    action_type: Mapped[str] = mapped_column(String(48))
    connector: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]]
    payload_digest: Mapped[str] = mapped_column(String(71))
    status: Mapped[str] = mapped_column(String(32))
    cancellation: Mapped[str] = mapped_column(String(24), default="none")
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    executed_by: Mapped[str | None] = mapped_column(String(200))
    remote_ref: Mapped[str | None] = mapped_column(String(200))
    remote_url: Mapped[str | None] = mapped_column(String(500))
    confirmation: Mapped[dict[str, Any] | None]
    failure_category: Mapped[str | None] = mapped_column(String(48))
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        _tenant_unique("operations"),
        UniqueConstraint("tenant_id", "operation_key", name="uq_operations_key"),
        UniqueConstraint("tenant_id", "approval_id", name="uq_operations_approval"),
        _fk(["case_id"], "cases", "fk_operations_case"),
        _fk(["proposal_id"], "proposals", "fk_operations_proposal"),
        _fk(["approval_id"], "approval_decisions", "fk_operations_approval"),
        CheckConstraint(
            "status in ('pending_dispatch','executing','confirmed','failed','outcome_unknown',"
            "'cancelled_before_dispatch')",
            name="ck_operations_status",
        ),
        CheckConstraint(
            "cancellation in ('none','requested','confirmed','unknown')",
            name="ck_operations_cancellation",
        ),
    )


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    step_id: Mapped[str] = mapped_column(String(48))
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retry_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # dispatch | reconcile | readback
    tool_name: Mapped[str] = mapped_column(String(64))
    connector_version: Mapped[str] = mapped_column(String(48))
    operation_key: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))
    error_category: Mapped[str | None] = mapped_column(String(48))
    error_detail: Mapped[str | None] = mapped_column(Text)
    input_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    output_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    input_digest: Mapped[str] = mapped_column(String(71))
    output_digest: Mapped[str | None] = mapped_column(String(71))
    response_mode: Mapped[str] = mapped_column(String(16))
    remote_ref: Mapped[str | None] = mapped_column(String(200))
    worker_id: Mapped[str | None] = mapped_column(String(120))
    fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None]
    __table_args__ = (
        _tenant_unique("attempts"),
        _fk(["operation_id"], "operations", "fk_attempts_operation"),
        _fk(["case_id"], "cases", "fk_attempts_case"),
        _fk(["parent_attempt_id"], "attempts", "fk_attempts_parent"),
        CheckConstraint(
            "status in ('started','succeeded','definite_failure','ambiguous_failure')",
            name="ck_attempts_status",
        ),
    )


# ---------------------------------------------------------------------------------------
# Jobs, outbox, audit
# ---------------------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None]
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        _tenant_unique("jobs"),
        CheckConstraint(
            "status in ('queued','leased','succeeded','failed','dead')", name="ck_jobs_status"
        ),
        Index("ix_jobs_claim", "status", "run_after", "priority"),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_type: Mapped[str] = mapped_column(String(48))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict[str, Any]]
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created()
    delivered_at: Mapped[datetime | None]
    __table_args__ = (
        _tenant_unique("outbox_events"),
        _fk(["job_id"], "jobs", "fk_outbox_events_job"),
    )


class AuditEvent(Base):
    """Append-only operational audit trail (application roles have no UPDATE/DELETE)."""

    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    occurred_at: Mapped[datetime] = created()
    actor_kind: Mapped[str] = mapped_column(String(16))  # user | service | system
    actor_id: Mapped[str] = mapped_column(String(200))
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(48))
    resource_id: Mapped[str] = mapped_column(String(100))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24), default="ok")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        Index("ix_audit_events_tenant_resource", "tenant_id", "resource_type", "resource_id"),
    )


# ---------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    suite_id: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(
        String(32)
    )  # rules_only | agent_mock | agent_recorded | agent_live
    split: Mapped[str] = mapped_column(String(24))
    settings: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(16))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    receipt: Mapped[dict[str, Any] | None]
    comparability: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None]
    __table_args__ = (_tenant_unique("evaluation_runs"),)


class EvaluationTrial(Base):
    __tablename__ = "evaluation_trials"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    eval_case_id: Mapped[str] = mapped_column(String(100))
    repeat_index: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(100))
    expected: Mapped[str] = mapped_column(String(100))
    correct: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        _tenant_unique("evaluation_trials"),
        _fk(["run_id"], "evaluation_runs", "fk_evaluation_trials_run"),
    )


# Tables whose rows belong to one tenant and are protected by the standard RLS policy.
TENANT_TABLES: tuple[str, ...] = (
    "tenant_settings",
    "tenant_counters",
    "policy_versions",
    "pack_versions",
    "artifacts",
    "source_documents",
    "document_versions",
    "evidence_chunks",
    "extracted_fields",
    "field_corrections",
    "suppliers",
    "purchase_orders",
    "po_lines",
    "invoices",
    "invoice_lines",
    "goods_receipts",
    "receipt_lines",
    "invoice_allocations",
    "supplier_assertions",
    "cases",
    "case_documents",
    "conversation_messages",
    "workflow_runs",
    "workflow_steps",
    "investigation_versions",
    "model_calls",
    "tool_invocations",
    "proposals",
    "approval_decisions",
    "operations",
    "attempts",
    "outbox_events",
    "audit_events",
    "evaluation_runs",
    "evaluation_trials",
)
