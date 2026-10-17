# Implementation plan

Owner: Venkata Manideep Patibandla. Spec: OPSBRIDGE_BUILD_PROMPT.md v1.0.
Branch: `feat/opsbridge-foundation`. Status of each item lives in `docs/STATUS.md` and
`docs/requirements-traceability.md`; this file is the plan, not evidence.

## Architecture sketch

```text
 Browser (React/Vite)                    Keycloak (OIDC, local dev profile)
     │  cookie session + CSRF header          ▲  auth code + PKCE
     ▼                                        │
 ┌──────────── api (FastAPI) ─────────────────┴──────┐
 │ auth/session · cases · evidence · proposals ·     │
 │ approvals · operations · evals · tenant settings  │
 └───────┬───────────────────────────────┬───────────┘
         │ SQL (opsbridge_app, RLS)      │ enqueue jobs (same txn)
         ▼                               ▼
   PostgreSQL 16 + pgvector  ◄──── worker (opsbridge_worker, leases, fencing)
   state · jobs · outbox ·            │ investigation steps / dispatch / reconcile
   audit · documents · FTS            │
                                      ├── model adapters: mock | anthropic | ollama
                                      ├── records-mcp  (MCP streamable HTTP, read tools)
                                      └── casework-mcp (MCP, follow-up task tools)
                                                │
                              ┌─────────────────┴─────────────────┐
                    purchasing simulator (HTTP,           GitHub Issues sandbox
                    SQLite, fault injection)               (allowlisted repo, opt-in)
```

Trust boundaries: browser ↔ api; api/worker ↔ PostgreSQL (non-bypass roles + RLS);
worker ↔ MCP services (service token + delegation token); MCP ↔ connectors
(allowlisted targets); worker ↔ model providers (redaction, routing policy, budget).

## First vertical slice (Phase 1 → 2 exit)

A logged-in furniture-tenant investigator opens case "INV-9001 blocked", OpsBridge
ingests PO-1001 / GR-5001 / INV-9001 / supplier email from the simulator, the
deterministic matcher reports a 2-unit quantity mismatch with citations, and an
IT-hardware tenant user receives 404 for that case through the API and zero rows through
the `opsbridge_app` database role.

Acceptance tests for the slice:
- `tests/domain/test_matching.py::test_twenty_ordered_eighteen_received`
- `tests/integration/test_tenant_isolation.py` (API + DB role)
- `tests/integration/test_case_persistence.py` (restart preserves case)

## Phases and gates

| Phase | Deliverable | Exit gate (automated where possible) |
|---|---|---|
| 0 | Inventory, discovery, ADRs, plan, traceability, status | Docs exist; first slice + data boundary explicit |
| 1 | API, DB + migrations + RLS roles, Keycloak, sessions, case intake + queue, web shell | Create/read own case; cross-tenant API + DB denial; restart persistence |
| 2 | Ingestion, evidence refs, 3 matching rules, corrections, case detail UI | 20/18 example correct with citations; missing → needs_information; rules baseline on seed corpus |
| 3 | Agent steps, mock/Anthropic/Ollama adapters, capabilities, budgets, prompt registry | Mock full flow; malformed output + injected instructions cannot act |
| 4 | Proposals, approvals, outbox/jobs, attempts, reconciliation, review/timeline UI | Stale/unauthorized approval fails; restart/duplicate tests; timeout-after-commit → one task or unknown |
| 5 | Records/Casework MCP, protocol auth, GitHub connector | Real MCP discovery/call/deny tests; GitHub contract tests |
| 6 | Redaction, retention, local-only routing, upload defences, pooled isolation, OCR path | Named adversarial tests pass; unverified paths labelled |
| 7 | Manifests, cassettes, strict replay, baselines, repeats, EvalSeal receipt | Tampered artifact fails verify; missing replay response fails closed |
| 8 | Telemetry, health, alerts, images, backup/restore, threat model, deployment plan | Clean local launch + restore drill observed |
| 9 | IT hardware pack, handoff docs | Core + isolation tests re-run for both packs without org branches |

## Ordering decisions
- Pure domain rules (money, units, tolerances, matching) are built first inside Phase 1
  because every later phase depends on them and they need no infrastructure.
- The purchasing simulator is built in Phase 2 (source records) and extended in Phase 4
  (task system with fault injection).
- MCP services wrap the same service-layer functions the API uses; policy checks live in
  the service layer so both entry points enforce them.

## Known external dependencies (may block only dependent work)
- Anthropic API key + explicit live opt-in + spend ceiling → hosted model live checks.
- Ollama installation + pulled model → local inference live checks (not installed now).
- GitHub sandbox repository + least-privilege token + authorization → live connector run.
- Tesseract (not installed) → OCR path; without it scanned PDFs go to needs_review.
- Cloud account + authorization → staging deployment.
- Real operators → pilot measurements.
