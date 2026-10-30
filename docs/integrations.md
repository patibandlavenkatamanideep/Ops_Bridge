# Integrations

Each integration lists what is implemented, what was verified, and how. "Verified" here
always means locally against the named counterpart; nothing below was run against a
production or customer system.

| Integration | Implemented | Verified | Live status |
|---|---|---|---|
| Keycloak (OIDC login, client credentials) | yes | real Keycloak 26.8.1 container | local only |
| Purchasing simulator (records) | yes | real HTTP, own process | synthetic stand-in, not an ERP |
| Simulator task tracker | yes | real HTTP with fault injection | synthetic stand-in |
| Records MCP / Casework MCP | yes | official SDK client↔server over streamable HTTP, real Keycloak tokens | local only |
| GitHub Issues sandbox connector | yes | HTTP contract tests against an independent fake of the documented API | **not run live** — needs sandbox repo, token and authorization |
| Anthropic / Ollama model adapters | yes | SDK/HTTP serialization against local fakes | **not run live** (see docs/agents-and-models.md) |

## MCP services (official SDK `mcp==2.3.0`, protocol 2026-07-28)

Transport: streamable HTTP (`/mcp`), stateless JSON responses. Run with
`python -m opsbridge.cli mcp-records` (:8101) and `mcp-casework` (:8102); the dev stack
starts both and routes tool calls through them when `OPSBRIDGE_TOOL_TRANSPORT=mcp`.

**Authorization (two credentials on every call; ADR 0006)**
1. `Authorization: Bearer` — Keycloak client-credentials token for `opsbridge-worker`,
   checked by `JwksTokenVerifier` (signature via JWKS, issuer, audience
   `opsbridge-records-mcp` / `opsbridge-casework-mcp`, expiry, caller allow-list). Missing or
   invalid tokens get HTTP 401 with an RFC 9728 `resource_metadata` challenge from the SDK.
   The SDK's URL-based resource check is disabled explicitly because Keycloak audiences are
   client ids; the verifier enforces the audience.
2. `X-OpsBridge-Delegation` — Ed25519 JWT signed by OpsBridge (≤ 5 min) naming tenant,
   principal, case/operation and allowed tools. The user's own token is never forwarded.

The service then re-reads the principal's current membership and permissions from
PostgreSQL (role `opsbridge_mcp`, RLS with the delegated tenant). Denials are SDK
`ToolError`s (caller sees `is_error` with a safe reason) and are audited with the caller
service, the principal and the delegation id.

**Records MCP tools** (permission `evidence.read`, case-scoped: results must be linked to
the delegated case): `get_purchase_order(po_number)`, `get_invoice(invoice_number)`,
`get_receipt_lines(po_number)`, `search_policy(topic)`,
`get_evidence_excerpt(version_id, locator, field)`, `search_case_evidence(query)`.

**Casework MCP tools** (caller must be `opsbridge-worker`; principal must be the operation's
approver and still hold `proposal.decide` with the action in scope):
`create_follow_up_task(operation_id)` — parameters are loaded from the approved operation,
never taken from arguments; requires status `executing` and an intact payload digest.
`get_follow_up_task(operation_id, remote_ref)` and `reconcile_operation(operation_id)`.
Business outcomes (`created`, `definite_failure`, `ambiguous_failure`, `searched`) are
returned as data so the executor applies its own definite/ambiguous rules.

Tests: `tests/integration/test_mcp.py` (discovery with/without token, wrong audience,
unlisted client, permitted calls, 7 denial cases, audit of denials, execution through
Casework MCP, timeout-after-commit behind the MCP hop, investigation search over Records
MCP).

## GitHub Issues sandbox connector (`connectors/github.py`)

Verified against GitHub REST documentation fetched 2026-10-14 (API version header
`X-GitHub-Api-Version: 2026-03-10`): [Issues](https://docs.github.com/en/rest/issues/issues),
[Search](https://docs.github.com/en/rest/search/search) (the source for "search can return
incomplete results") and [permissions required for fine-grained personal access
tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).
Re-check them before a live run; the pages are not copied into this repository.

| Use | Endpoint | Fine-grained token permission |
|---|---|---|
| Create follow-up issue | `POST /repos/{owner}/{repo}/issues` (201) | Issues: **write** (repository) |
| Read back | `GET /repos/{owner}/{repo}/issues/{issue_number}` | Issues: read |
| Reconcile by marker | `GET /repos/{owner}/{repo}/issues?state=all&since=…&sort=created&direction=desc&per_page=100` | Issues: read |

Capability declaration: no idempotency keys (GitHub has none for issue creation);
lookup **eventual** (listing is not documented as strongly consistent; search can return
incomplete results); no conditional writes. Definite: 400/401/403/404/410/422 (403/429 with
rate-limit headers are retryable). Ambiguous: timeouts, dropped connections, 5xx. After an
ambiguous create, a lookup that finds nothing is inconclusive → `outcome_unknown` +
human review; no automatic re-create.

Where tasks go is the tenant's **task connection** (Tenant settings → Task connection;
`services/connections.py`, migration 0017): connector, repository and a credential
reference, versioned and audited. Safety rails: the repository must be on the deployment's
allowlist (`OPSBRIDGE_GITHUB_REPOSITORY_ALLOWLIST`, JSON list; the sandbox repository
`OPSBRIDGE_GITHUB_SANDBOX_REPO` is always included), refused otherwise; the credential is a
secret in the deployment's store, named by the connection's reference and read from
`OPSBRIDGE_CONNECTION_SECRET_<REF>` (upper case, `-` as `_`), or `OPSBRIDGE_GITHUB_TOKEN` when
the connection names none; a reference must be registered by the deployment for that tenant,
connector and repository (`OPSBRIDGE_CONNECTION_CREDENTIALS`), checked when the connection is
set and again before the secret is read, so a tenant cannot pick another tenant's credential
(production review A06); writes refused unless `OPSBRIDGE_GITHUB_LIVE_WRITES_AUTHORIZED=true`;
only synthetic, redacted text plus an opaque marker is sent; tenants can use it only on a tier
that enables `github_issues`. Approvals bind the repository: moving the connection makes
pending approvals stale, and dispatch recovers an earlier write in the repository it was
approved for, with that version's credential (not one that was revoked).

Tests: `tests/contracts/test_github_connector.py` (10, independent fake server written from
the docs) and `tests/integration/test_github_execution.py` (executor + connector: confirmed
create; timeout-after-commit with lagging listing → `outcome_unknown` with exactly one
issue; reconciliation when the listing shows it).

**To run live (requires the owner's authorization):** create a sandbox repository, a
fine-grained token limited to that repository with Issues read/write, then
`OPSBRIDGE_GITHUB_SANDBOX_REPO=owner/name OPSBRIDGE_GITHUB_TOKEN=… OPSBRIDGE_GITHUB_LIVE_WRITES_AUTHORIZED=true
OPSBRIDGE_RUN_LIVE=github uv --directory backend run pytest tests/live -q`. Until that run
happens, the connector is **not** live-verified.

## Purchasing exports (`connectors/exports.py`)

Reads an organization's export (`OPSBRIDGE_PURCHASING_EXPORT_DIR/<organization>/`: files plus
a `manifest.json`, `opsbridge-export/1`) through the same `purchasing-source/1` contract as the
simulator connector. Files are checked against their SHA-256 digests; untrusted exports
(paths outside the export, repeated identities, mixed snapshots, unknown schema) are refused;
the investigation's read time is the export's `extracted_at`. Format and rules:
docs/integration-onboarding.md, "Exports". Tested only with hand-written exports.

## Purchasing simulator

A separate FastAPI service with its own SQLite store, per-org HMAC credentials and native
record formats per organization (`simulator/`). Fault controls (`/_control/*`: delays,
definite/ambiguous failures, malformed records, timeout-after-commit, capability toggles)
exist only when enabled and are refused in production mode. It stands in for an external
purchasing system; it is not an integration with any real ERP.
