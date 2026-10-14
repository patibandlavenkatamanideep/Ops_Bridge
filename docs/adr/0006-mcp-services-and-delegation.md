# ADR 0006: Two MCP services with service tokens plus a delegated authorization context

Status: accepted (2026-10-14)

## Decision
- `records-mcp` exposes read tools (get_purchase_order, get_invoice, get_receipt_lines,
  search_policy, get_evidence_excerpt). `casework-mcp` exposes create_follow_up_task,
  get_follow_up_task, reconcile_operation.
- Transport: streamable HTTP from the official SDK (mcp 2.3.0). Callers present an
  OAuth bearer token for the MCP resource (audience-checked by a `TokenVerifier`).
- The human principal and tenant are carried in a separate short-lived delegation token
  (JWT, EdDSA/Ed25519 via PyJWT + cryptography; standard algorithms, no custom crypto)
  signed by the OpsBridge backend, naming tenant, principal, case, run, allowed tools,
  and expiry. The MCP service verifies both tokens, then applies the shared policy service
  and RLS. A user's own access token is never forwarded.
- `create_follow_up_task` additionally requires an approved, unconsumed operation id in the
  delegation context; the service re-checks it in the database.

## Consequences
- Local tests can mint service tokens from a test JWKS; integration tests use Keycloak.
- Delegation tokens are bearer credentials; lifetime is short (default 5 min) and they are
  bound to an audience per MCP service.
