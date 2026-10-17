# ADR 0003: Tenant isolation with PostgreSQL row-level security

Status: accepted (2026-10-14)

## Decision
- Every tenant-owned table has `tenant_id` and a composite unique key `(tenant_id, id)`.
  Child foreign keys reference `(tenant_id, parent_id)` so a row cannot point at a parent
  in another tenant.
- RLS is `ENABLE`d and `FORCE`d on tenant-owned tables. Policies compare `tenant_id` with
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid`. If the setting is absent, no
  rows are visible and inserts fail.
- The tenant context is set per transaction with `set_config('app.tenant_id', $1, true)`
  (transaction-local), so it disappears at COMMIT/ROLLBACK and cannot leak to the next
  borrower of a pooled connection.
- Roles: `opsbridge_migrator` owns the schema; `opsbridge_app` (API), `opsbridge_worker`
  (jobs) and `opsbridge_mcp` (MCP services) are `NOSUPERUSER NOBYPASSRLS` and are not
  table owners.
- Cross-tenant job claiming: the worker role may read and lease rows in `jobs` for all
  tenants (job rows hold identifiers only). Business tables remain tenant-scoped; the
  worker sets the job's tenant context before touching them.
- Membership lookup before a tenant is selected uses a policy on `app.user_id`
  (a user can see their own memberships).

## Consequences
- The application still checks authorization in handlers and services; RLS is the backstop.
- A database owner or superuser can bypass RLS; operational access to those roles must be
  restricted. We do not claim RLS protects against a database administrator.
