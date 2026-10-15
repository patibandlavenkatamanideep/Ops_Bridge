# Security and threat model

Status: covers the local build through Phase 6. Deployment-specific threats (TLS
termination, WAF, secret manager, network policy) are added with the staging plan in Phase 8.
"Evidence" names the automated test that exercises the control; a control without a test is
marked as such.

## Assets

1. Tenant business data: purchase orders, receipts, invoices, supplier messages, uploads.
2. Decisions and their integrity: findings, citations, approvals, operations, audit trail.
3. Authority: user sessions, approval rights, worker/MCP service credentials, delegation keys.
4. External effects: follow-up tasks created in a task system (simulator or GitHub sandbox).
5. Secrets: OIDC client secrets, simulator master secret, delegation signing key, API keys.

## Actors and trust boundaries

| Actor | Trusted for | Not trusted for |
|---|---|---|
| Signed-in user (browser) | Actions their server-side membership permits | Tenant id, role, tier, approver identity in any request field |
| Another tenant's user | Nothing in this tenant | — |
| Supplier / document author | Being the source of a document | Any instruction inside it; field values beyond what parsing and rules accept |
| Model (mock, hosted, local) | Proposing text and evidence requests | Arithmetic, permissions, action parameters, conclusions without citations |
| Worker / MCP services | Their own scoped service identity | Acting as a user beyond a signed, short-lived delegation |
| Source and task systems | Returning data and outcomes | Consistency after a timeout (reconciled, or marked `outcome_unknown`) |

Boundaries: browser ⇄ API (cookie session + CSRF); API/worker ⇄ PostgreSQL (non-bypass RLS
roles); worker ⇄ MCP services (Keycloak service token + Ed25519 delegation); worker/MCP ⇄
simulator / GitHub (HMAC or token, allow-listed repository); hosted model provider
(redacted prompts, opt-in, spend ceiling).

## Threats, controls, evidence

| # | Threat | Control | Evidence | Residual risk |
|---|---|---|---|---|
| T1 | Cross-tenant read via guessed ids | Tenant from verified session + membership; RLS FORCE on every table; composite tenant FKs; 404 not 403 | `test_rls.py` (12), `test_other_tenant_cannot_access_case_by_any_route`, `e2e/phase1.spec.ts` | Bugs in a future table without a policy; `alembic check` + RLS tests catch drift only for covered tables |
| T2 | Tenant context leaking through pooled connections | Transaction-local `set_config`, re-applied after every BEGIN; single-connection pool test across tenants and "no tenant" | `test_rls.py` pooled tests, `test_hardening.py::test_worker_connection_reuse_does_not_carry_tenant_context` | — |
| T3 | Retrieval ranks or returns another tenant's text | Authorization (RLS + allowed version ids) in the SQL `WHERE`, before `ts_rank` | `test_retrieval_authorizes_before_ranking_across_tenants` | Lexical only; semantic retrieval not built, so its isolation is untested |
| T4 | Artifact path traversal / cross-tenant artifact read | Keys are `<tenant>/<aa>/<sha256>`, validated and resolved inside the tenant root | `test_artifact_keys_cannot_cross_tenants_or_escape_the_store` | Local filesystem store; staging needs object-store IAM per tenant prefix |
| T5 | Cached sensitive responses | `Cache-Control: no-store` on `/api` and `/auth`; downloads are `attachment` + `nosniff`; no shared server cache exists | `test_original_downloads_are_never_cached_or_rendered_inline` | Browser memory cache (React Query) is cleared on sign-out by a full reload |
| T6 | Malicious upload (oversize, disguised HTML, traversal filename, parser bomb) | Content-Length cap before buffering, 411 for chunked bodies, content sniffing vs declared type, safe basename, bounded parsers, PDF parsing in a killable subprocess | `test_upload_*`, `test_chunked_body_without_length_is_refused_before_buffering`, `tests/evidence/test_parsers.py` | No antivirus scanning; PDFs are parsed, never rendered server-side |
| T7 | Prompt injection in documents | Documents are data; model output validated (schema, citations exist, numbers supported, prohibited claims, route agreement); action parameters loaded server-side; approvals required | `test_injected_document_instructions_cannot_change_authority`, `tests/agents/test_routing_and_validation.py` | A validated but misleading explanation can still reach a human; the approval step is the control |
| T8 | CSRF / cross-origin abuse | Synchronizer token + Origin check on cookie writes; CORS allow-list; SameSite=Lax cookie | `test_csrf_token_required_for_writes`, `test_cors_allows_only_configured_origin` | — |
| T9 | Session theft | HttpOnly cookie with hashed server-side session, 8 h absolute / 30 min idle, logout revokes | `test_oidc_login.py` | Local dev runs on http; staging must set Secure cookies behind TLS |
| T10 | Stale or replayed approval executes | Approval bound to version, payload digest, policy, source manifest, expiry; rechecked at dispatch; single consumption | `test_approvals_execution.py` (18), `test_policy_published_before_dispatch_cancels_the_operation` | — |
| T11 | Duplicate external effect after timeout | Attempt ledger before the call; capability-driven reconciliation; `outcome_unknown` + human review when lookup is eventual/absent | `test_approvals_execution.py` timeout-after-commit, `test_github_execution.py` | At-least-once delivery; exactly-once is not claimed |
| T12 | Service identity used as a user | Two credentials per MCP call (service token audience + allow-list, delegation naming tenant, principal, case/operation, tools; ≤ 5 min); membership re-read server-side | `test_mcp.py` (7 denial cases) | Delegation signing key is a local file in dev; staging needs a managed key |
| T13 | Tier used as authority | Tiers gate features only; roles and membership gate actions | `test_tenant_settings_respect_roles_and_tiers`, `test_tier_entitlement_gates_model_modes` | — |
| T14 | Secrets or identifiers in logs | Redacting filter on every handler incl. uvicorn access logs (OIDC `code`/`state`) and tracebacks | `tests/privacy/test_log_redaction.py` | Pattern-based; telemetry export not built yet (Phase 8) |
| T15 | Sensitive data sent to a hosted model | Opt-in per run, tier gate, pattern redaction, local-required policy with no hosted fallback, spend ceiling | `tests/agents`, `test_local_required_policy_blocks_model_without_hosted_fallback` | Redaction is not anonymization |
| T16 | Retained content kept longer than policy | Retention sweep deletes bytes/text and records the deletion | `test_retention_deletes_protected_content_and_says_so` | No automatic schedule; backups not covered |
| T17 | Changed source data after investigation | Source manifest digest; new document versions only when bytes change; re-investigation shows the new version | `test_changed_source_document_creates_a_new_version_and_new_evidence_state` | Changes between investigation and approval are caught at dispatch via the manifest |
| T18 | Abuse / brute force | Per-session/IP fixed-window rate limit, body-size limit | `test_rate_limit_and_body_limit` | In-memory, per process; not a DDoS control |
| T19 | Forbidden autonomous actions | No payment, invoice posting, bank-detail or PO-modification actions exist; the action registry contains follow-up tasks and local case actions only | code review (`connectors/`, `services/proposals.py`) | — |

## Out of scope for v1

Custom identity provider, WAF/DDoS protection, HSM-backed keys, malware scanning, data-loss
prevention, formal compliance claims. These are deployment or organizational controls and
are listed in the staging plan as prerequisites, not as implemented features.
