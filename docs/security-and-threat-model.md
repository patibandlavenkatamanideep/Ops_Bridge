# Security and threat model

Status: covers the local build through Phase 8 (local containers and CI definition
included). Cloud-specific controls (WAF, managed keys, network policy) are planned in
docs/deployment.md and are not implemented. "Evidence" names the automated test or drill
that exercises the control; a control without one is marked as such.

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
| T4 | Artifact path traversal / cross-tenant artifact read | Keys are `<tenant>/<aa>/<sha256>`, validated and resolved inside the tenant root | `test_artifact_keys_cannot_cross_tenants_or_escape_the_store` | Same keys and checks in the S3 store (`tests/evidence/test_s3_store.py`, `tests/integration/test_s3_store.py`); one bucket and task role serve all tenants, so separation relies on these checks and the RLS-protected manifest, not per-tenant IAM |
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
| T16 | Retained content kept longer than policy | Retention sweep deletes bytes/text and records the deletion | `test_retention_deletes_protected_content_and_says_so`, `test_retention_schedule.py`, `test_retention_uses_the_database_clock_not_a_drifting_app_clock` | Backups not covered |
| T17 | Changed source data after investigation | Source manifest digest; new document versions only when bytes change; re-investigation shows the new version | `test_changed_source_document_creates_a_new_version_and_new_evidence_state` | Changes between investigation and approval are caught at dispatch via the manifest |
| T18 | Abuse / brute force | Per-session/IP fixed-window rate limit, body-size limit | `test_rate_limit_and_body_limit` | In-memory, per process; not a DDoS control |
| T19 | Forbidden autonomous actions | No payment, invoice posting, bank-detail or PO-modification actions exist; the action registry contains follow-up tasks and local case actions only | code review (`connectors/`, `services/proposals.py`) | — |
| T20 | Telemetry leaks content or identifiers | Spans carry ids, route templates and statuses only (string attributes scrubbed); metric labels bounded (no tenant/case/user ids); logs scrubbed; FastAPI native telemetry disabled | `test_observability.py` (no document text in spans, no UUIDs in labels) | A new span attribute added carelessly is only pattern-scrubbed |
| T21 | Forged trace context from the internet | Each request starts a new trace; `traceparent` from clients is ignored | `test_incoming_trace_context_is_not_trusted` | Internal propagation (API → job) is trusted |
| T22 | Cross-tenant aggregates weaken RLS | `ops.metrics_snapshot()` is SECURITY DEFINER, owned by NOLOGIN `opsbridge_ops_reader` with column-level SELECT on status/timestamp columns; returns counts and ages only; app/worker may execute it, nothing else | `test_aggregate_state_reports_backlog_while_rls_still_hides_rows` | Counts across tenants are visible to operators of the metrics endpoint |
| T23 | `/metrics` scraped by outsiders | Aggregate data only; optional bearer token; bound to localhost locally and to the private network in the staging plan | `test_metrics_endpoint_can_require_a_token` | Token is static; rotate with the deployment |
| T24 | Secrets in images or CI | Allow-list build context (no `.env`, keys, `var/`); CI uses no secrets, `contents: read`, actions pinned to SHAs, runs on PRs to main or manual dispatch | image inspected (no `.env`); actionlint; hosted CI runs without secrets (docs/STATUS.md) | Runner images and apt packages are not pinned |
| T26 | Hostile image in a scanned PDF (decoder bug, decompression bomb, slow OCR) | OCR is off by default. Images are decoded in the PDF worker subprocess (hard timeout) with a 40 MP pixel cap, at most 4 images per page and `ocr_max_pages` pages; tesseract runs with a per-page timeout, one thread, a fixed argument vector and a validated language code, as uid 10001 in a read-only container | `test_an_oversized_image_is_refused_not_decoded`, `test_language_codes_are_validated_before_reaching_the_command_line` | Pillow and tesseract parse untrusted image data in native code; no extra sandbox (seccomp) around them |
| T27 | OCR misreading becomes a business fact | OCR text stays `needs_review`; OCR field values are candidates (`ocr` / `needs_review`), never records; corrections of them are refused | `test_ocr_values_are_shown_for_review_but_never_decide` (a perfect reading of INV-9001 still goes to a person); real OCR misread `ST-150` as `ST-15@` | A person may still act on a misread value they see; the UI labels it |
| T25 | Container compromise blast radius | Non-root uid 10001; read-only root filesystem with `/tmp` tmpfs; all default capabilities dropped; `no-new-privileges`; only the `/app/var` volume writable; internal services unpublished; MCP DNS-rebinding protection with explicit allowed hosts | hardened clean-launch drill: root-filesystem write refused, 12 browser tests passed (docs/operations.md) | No custom seccomp/AppArmor profile beyond engine defaults; the shared `/app/var` volume is writable by every app container |

## Out of scope for v1

Custom identity provider, WAF/DDoS protection, HSM-backed keys, malware scanning, data-loss
prevention, formal compliance claims. These are deployment or organizational controls and
are listed in the staging plan as prerequisites, not as implemented features.
