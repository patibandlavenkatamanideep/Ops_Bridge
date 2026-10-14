"""Row-level security policies and least-privilege grants.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14

Tenant context comes from transaction-local settings (set_config(..., true)):
  app.tenant_id, app.user_id, app.session_hash, app.oidc_issuer, app.oidc_subject.
An unset context matches no rows. Application roles are neither superusers, table
owners, nor BYPASSRLS, and FORCE ROW LEVEL SECURITY also binds the owner.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLES = ("opsbridge_app", "opsbridge_worker", "opsbridge_mcp")

# Standard tenant-isolated tables (policy: tenant_id = app_current_tenant()).
TENANT_TABLES = (
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

# Per-role table privileges beyond SELECT. Audit events are append-only for everyone.
APP_WRITE = {
    "tenant_counters": "INSERT, UPDATE",
    "source_documents": "INSERT, UPDATE",
    "document_versions": "INSERT, UPDATE",
    "artifacts": "INSERT, UPDATE",
    "evidence_chunks": "INSERT",
    "extracted_fields": "INSERT, UPDATE",
    "field_corrections": "INSERT",
    "suppliers": "INSERT, UPDATE",
    "purchase_orders": "INSERT, UPDATE",
    "po_lines": "INSERT",
    "invoices": "INSERT, UPDATE",
    "invoice_lines": "INSERT",
    "goods_receipts": "INSERT, UPDATE",
    "receipt_lines": "INSERT",
    "invoice_allocations": "INSERT, UPDATE",
    "supplier_assertions": "INSERT, UPDATE",
    "cases": "INSERT, UPDATE",
    "case_documents": "INSERT, DELETE",
    "conversation_messages": "INSERT, UPDATE",
    "workflow_runs": "INSERT, UPDATE",
    "proposals": "INSERT, UPDATE",
    "approval_decisions": "INSERT",
    "operations": "INSERT, UPDATE",
    "outbox_events": "INSERT",
    "audit_events": "INSERT",
    "policy_versions": "INSERT",
    "pack_versions": "INSERT",
    "evaluation_runs": "INSERT, UPDATE",
    "evaluation_trials": "INSERT",
}
WORKER_WRITE = {t: "INSERT, UPDATE" for t in TENANT_TABLES if t != "audit_events"} | {
    "audit_events": "INSERT",
    "evidence_chunks": "INSERT, UPDATE, DELETE",
    "case_documents": "INSERT, DELETE",
}
MCP_WRITE = {
    "tool_invocations": "INSERT, UPDATE",
    "audit_events": "INSERT",
}


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app_current_tenant() RETURNS uuid LANGUAGE sql STABLE AS
          $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;
        CREATE FUNCTION app_current_user() RETURNS uuid LANGUAGE sql STABLE AS
          $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
        CREATE FUNCTION app_session_hash() RETURNS text LANGUAGE sql STABLE AS
          $$ SELECT NULLIF(current_setting('app.session_hash', true), '') $$;
        CREATE FUNCTION app_oidc_issuer() RETURNS text LANGUAGE sql STABLE AS
          $$ SELECT NULLIF(current_setting('app.oidc_issuer', true), '') $$;
        CREATE FUNCTION app_oidc_subject() RETURNS text LANGUAGE sql STABLE AS
          $$ SELECT NULLIF(current_setting('app.oidc_subject', true), '') $$;
        """
    )
    roles = ", ".join(APP_ROLES)
    op.execute(
        f"GRANT EXECUTE ON FUNCTION app_current_tenant(), app_current_user(), app_session_hash(), "
        f"app_oidc_issuer(), app_oidc_subject() TO {roles}"
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = app_current_tenant()) "
            "WITH CHECK (tenant_id = app_current_tenant())"
        )
        op.execute(f"GRANT SELECT ON {table} TO {roles}")
        if table in APP_WRITE:
            op.execute(f"GRANT {APP_WRITE[table]} ON {table} TO opsbridge_app")
        if table in WORKER_WRITE:
            op.execute(f"GRANT {WORKER_WRITE[table]} ON {table} TO opsbridge_worker")
        if table in MCP_WRITE:
            op.execute(f"GRANT {MCP_WRITE[table]} ON {table} TO opsbridge_mcp")

    # --- tenants: visible when it is the current tenant or the user is a member.
    op.execute(
        """
        ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_visible ON tenants FOR SELECT USING (
          id = app_current_tenant()
          OR EXISTS (SELECT 1 FROM memberships m
                     WHERE m.tenant_id = tenants.id AND m.user_id = app_current_user()
                       AND m.status = 'active'));
        CREATE POLICY tenant_bootstrap ON tenants TO opsbridge_migrator USING (true) WITH CHECK (true);
        """
    )
    op.execute(f"GRANT SELECT ON tenants TO {roles}")

    # --- users: self, the identity being logged in, or co-members of the current tenant.
    op.execute(
        """
        ALTER TABLE users ENABLE ROW LEVEL SECURITY;
        ALTER TABLE users FORCE ROW LEVEL SECURITY;
        CREATE POLICY user_visible ON users FOR SELECT USING (
          id = app_current_user()
          OR (issuer = app_oidc_issuer() AND subject = app_oidc_subject())
          OR EXISTS (SELECT 1 FROM memberships m
                     WHERE m.user_id = users.id AND m.tenant_id = app_current_tenant()));
        CREATE POLICY user_login_insert ON users FOR INSERT TO opsbridge_app
          WITH CHECK (issuer = app_oidc_issuer() AND subject = app_oidc_subject());
        CREATE POLICY user_login_update ON users FOR UPDATE TO opsbridge_app
          USING (issuer = app_oidc_issuer() AND subject = app_oidc_subject())
          WITH CHECK (issuer = app_oidc_issuer() AND subject = app_oidc_subject());
        CREATE POLICY user_bootstrap ON users TO opsbridge_migrator USING (true) WITH CHECK (true);
        """
    )
    op.execute(f"GRANT SELECT ON users TO {roles}")
    op.execute("GRANT INSERT, UPDATE ON users TO opsbridge_app")

    # --- memberships: own memberships, or all memberships of the current tenant.
    op.execute(
        """
        ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
        CREATE POLICY membership_visible ON memberships FOR SELECT USING (
          tenant_id = app_current_tenant() OR user_id = app_current_user());
        CREATE POLICY membership_write ON memberships FOR INSERT TO opsbridge_app
          WITH CHECK (tenant_id = app_current_tenant());
        CREATE POLICY membership_update ON memberships FOR UPDATE TO opsbridge_app
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
        CREATE POLICY membership_bootstrap ON memberships TO opsbridge_migrator
          USING (true) WITH CHECK (true);
        """
    )
    op.execute(f"GRANT SELECT ON memberships TO {roles}")
    op.execute("GRANT INSERT, UPDATE ON memberships TO opsbridge_app")

    # --- user_sessions: only the session whose hash is in the transaction context.
    op.execute(
        """
        ALTER TABLE user_sessions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE user_sessions FORCE ROW LEVEL SECURITY;
        CREATE POLICY session_by_hash ON user_sessions TO opsbridge_app
          USING (id = app_session_hash()) WITH CHECK (id = app_session_hash());
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON user_sessions TO opsbridge_app")

    # --- service identities: non-sensitive registry readable by application roles.
    op.execute(
        """
        ALTER TABLE service_identities ENABLE ROW LEVEL SECURITY;
        ALTER TABLE service_identities FORCE ROW LEVEL SECURITY;
        CREATE POLICY service_identity_read ON service_identities FOR SELECT USING (true);
        CREATE POLICY service_identity_bootstrap ON service_identities TO opsbridge_migrator
          USING (true) WITH CHECK (true);
        """
    )
    op.execute(f"GRANT SELECT ON service_identities TO {roles}")

    # --- jobs: tenant-scoped for API/MCP; the worker may claim across tenants.
    #     Job payloads carry identifiers only; business rows stay tenant-scoped.
    op.execute(
        """
        ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY jobs_tenant ON jobs TO opsbridge_app, opsbridge_mcp
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
        CREATE POLICY jobs_worker_claim ON jobs TO opsbridge_worker USING (true)
          WITH CHECK (tenant_id = app_current_tenant() OR app_current_tenant() IS NULL);
        """
    )
    op.execute(f"GRANT SELECT ON jobs TO {roles}")
    op.execute("GRANT INSERT ON jobs TO opsbridge_app")
    op.execute("GRANT INSERT, UPDATE ON jobs TO opsbridge_worker")

    # Bootstrap privileges for the seeding path (migrator owns tables; FORCE RLS still
    # applies, so seeding sets tenant context like every other writer).


def downgrade() -> None:
    for table in (
        *TENANT_TABLES,
        "tenants",
        "users",
        "memberships",
        "user_sessions",
        "service_identities",
        "jobs",
    ):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM opsbridge_app, opsbridge_worker, opsbridge_mcp")
    op.execute(
        """
        DROP POLICY IF EXISTS tenant_visible ON tenants;
        DROP POLICY IF EXISTS tenant_bootstrap ON tenants;
        DROP POLICY IF EXISTS user_visible ON users;
        DROP POLICY IF EXISTS user_login_insert ON users;
        DROP POLICY IF EXISTS user_login_update ON users;
        DROP POLICY IF EXISTS user_bootstrap ON users;
        DROP POLICY IF EXISTS membership_visible ON memberships;
        DROP POLICY IF EXISTS membership_write ON memberships;
        DROP POLICY IF EXISTS membership_update ON memberships;
        DROP POLICY IF EXISTS membership_bootstrap ON memberships;
        DROP POLICY IF EXISTS session_by_hash ON user_sessions;
        DROP POLICY IF EXISTS service_identity_read ON service_identities;
        DROP POLICY IF EXISTS service_identity_bootstrap ON service_identities;
        DROP POLICY IF EXISTS jobs_tenant ON jobs;
        DROP POLICY IF EXISTS jobs_worker_claim ON jobs;
        """
    )
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(
        "DROP FUNCTION app_current_tenant(), app_current_user(), app_session_hash(), "
        "app_oidc_issuer(), app_oidc_subject()"
    )
