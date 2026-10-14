# OpsBridge status

Last updated: 2026-10-14 · Branch: `feat/opsbridge-foundation` (pushed to
github.com/patibandlavenkatamanideep/Ops_Bridge; never pushed to `main`) ·
Working copy: `~/Projects/Ops_Bridge` (see docs/adr/0009).

## Summary by validation level

| Level | State |
|---|---|
| Local application complete | no — Phases 0–3 done; Phase 4 next |
| Real model validated | not evaluated (no live opt-in, no credentials) |
| External connector (GitHub sandbox) validated | not evaluated (no sandbox repo/token/authorization for writes) |
| Hosted CI | not run (no workflow pushed yet) |
| Staging deployed | no |
| Operator pilot measured | no |

## Workspace inventory (Phase 0, 2026-10-14)

- Initial contents: `CLAUDE.md`, `OPSBRIDGE_BUILD_PROMPT.md`. No repository, code or tests.
- Toolchain: uv 0.10.2 (CPython 3.12.12), Node 25.9.0 / npm 11.12.1, Docker CLI 29.8.1 +
  Compose 5.5.1 (Docker Desktop daemon **not running**), Podman 5.7.1 machine running with a
  Docker-compatible socket, GNU Make 3.81, git 2.50.1, gh 2.92.0. Homebrew PostgreSQL 16 on
  :5432 hosts unrelated databases and is never used by OpsBridge.
- Missing: Java (Keycloak runs in a container), Ollama, Tesseract OCR.
- The owner created the public GitHub repo during Phase 1 and authorized pushing the
  feature branch after each phase. Its initial commit (MIT LICENSE, README, .gitignore) was
  merged, not rewritten.

## Completed

### Phase 0 — audit, contract, discovery assumptions
- docs/discovery.md, docs/IMPLEMENTATION_PLAN.md, docs/requirements-traceability.md,
  ADRs 0001–0009.
- Upstream interfaces inspected locally: evalseal 2.2.0 (API, CLI, docs at tag v2.2.0),
  mcp 2.3.0 (`MCPServer`, `Client`, `streamable_http_client`, `TokenVerifier`),
  Authlib 1.9.1 Starlette client (PKCE, nonce, ID-token validation path).

### Phase 1 — authenticated skeleton and durable data (exit gate passed locally)
- Domain: Decimal money, units, versioned policies, three-way matcher, rules-only routing
  (`backend/src/opsbridge/domain/`), furniture and IT policy packs (`packs/`).
- Infra: `infra/compose/docker-compose.yml` (pgvector PG16 with baked role init on :5433,
  Keycloak 26.8.1 on :8081 with realm import using env placeholders).
- Persistence: 40 tables, composite tenant FKs, Alembic 0001 (schema) + 0002 (RLS on all
  40 tables, FORCE RLS, least-privilege grants; app/worker/mcp roles are NOSUPERUSER
  NOBYPASSRLS non-owners). Transaction-local tenant context re-applied on every transaction.
- Identity: OIDC Authorization Code + PKCE via Authlib against Keycloak; server-side hashed
  sessions (absolute 8 h, idle 30 min), CSRF synchronizer token + Origin check, RP-initiated
  logout; worker client-credentials identity with MCP audiences.
- API: `/api/v1/me`, case intake (idempotency key, duplicate-invoice guard), keyset-paginated
  queue, case detail; consistent error bodies; correlation IDs; security headers; body-size
  and per-session/IP rate limits; liveness/readiness.
- UI: React 19/Vite 8 shell with sign-in, tenant switcher, case queue (loading/empty/
  denied/error states), new case form, case detail; types generated from OpenAPI.
- Seed: `make seed-demo` idempotently creates only the two designated synthetic tenants.
- Commands: Makefile (`doctor setup infra-up migrate seed-demo dev demo lint typecheck test
  test-integration test-e2e verify build down reset-demo openapi`); direct equivalents in
  docs/developer-commands.md. `eval-offline` and `demo --seed-records` targets exist but
  depend on Phase 2/7 commands not yet implemented.

### Phase 2 — deterministic investigation (exit gate passed locally)
- Purchasing simulator (`backend/src/opsbridge/simulator/`): separate FastAPI service, SQLite
  store, per-org HMAC credentials, native record formats per organization, task tracker and
  fault-injection controls (refused in production mode).
- Bounded parsers (`evidence/parsers.py`): JSON/CSV/text/PDF with type sniffing, size/depth/
  row/page limits; PDFs parsed in a killable subprocess (`evidence/pdf_worker.py`); image-only
  PDFs → `needs_review` (OCR unavailable, never invented); active PDF content flagged.
- Ingestion (`evidence/ingest.py`): raw bytes + raw SHA-256 in tenant-scoped artifact storage,
  separate normalized digest (`opsbridge-norm/1`), versioning only when bytes change, chunks
  with page/line/row locators, extracted fields, canonical records with superseding.
- Pack mappings (`packs/*/pack.yaml`, `mapping.py`): furniture (snake_case JSON, CSV, PDF
  text patterns with amount checks, email assertions) and IT (camelCase JSON) through one
  generic engine.
- Investigation workflow (`workflow/investigation.py`) with durable steps
  collect_context → retrieve_policy → reconcile_records → explain_findings; leased job queue
  with fencing tokens (`workflow/jobs.py`); worker (`workflow/worker.py`).
- Citations (`evidence/citations.py`) resolved against tenant, version, digest and locator.
- Corrections keep the original extraction; latest correction applied as an overlay and
  bound into the investigation's source manifest. Evidence-based resolution guard.
- UI: investigation tab (outcome, explanation, uncertainty, cited findings, run progress)
  and evidence tab (documents/versions, fields beside source text, highlighted citation,
  correction form, original download).
- Corpus: `fixtures/scenarios/dev.yaml` — 37 hand-labelled synthetic scenarios (25
  furniture, 12 IT) authored in `author_dev.py`. Rules-only baseline:
  `uv --directory backend run python -m opsbridge.cli eval-rules` →
  `evals/results/rules-baseline-dev.json`: **37/37 case-correct on the development split**.
  This is development verification: the labels and rules were written together; it is not
  held-out evidence and says nothing about model quality.

### Phase 3 — bounded agent assistance and model adapters (exit gate passed locally)
- Model contract and adapters (`agents/contracts.py`, `agents/adapters.py`): deterministic
  mock (labelled mocked), Anthropic via official SDK 1.13.0 (`output_config` JSON schema),
  Ollama via `/api/chat` (`format` schema); unsupported parameters rejected.
- Routing, budgets, spend ceiling and recording (`agents/runtime.py`); prompt registry with
  digests (`agents/prompts.py`); validators and capped read-only evidence search
  (`agents/steps.py`); pattern redaction (`privacy/redaction.py`); authorized PostgreSQL
  full-text retrieval (`evidence/retrieval.py`, lexical only).
- Workflow now: collect_context → retrieve_policy → reconcile_records → explain_findings →
  propose_follow_up → validate_proposal (`investigation/1.1.0`). Tenant settings
  (migration 0003) hold the default model mode; furniture = mock, IT = ollama.
- UI: model-mode selector (tier-limited), explanation origin, gated draft follow-up.
- Details and limits: docs/agents-and-models.md.

Phase 3 exit gate evidence:
- Mock-backed full flow: `tests/integration/test_agent_flow.py::test_mock_backed_full_flow_records_model_and_tool_calls`, `frontend/e2e/phase3.spec.ts`.
- Malformed output / injected instructions cannot act: `test_injected_document_instructions_cannot_change_authority`
  (compromised scripted model), `test_model_failure_preserves_deterministic_findings`,
  `tests/agents/test_routing_and_validation.py` (15 rejection cases).
- Local-required with no local model → blocked, no hosted fallback: `test_local_required_policy_blocks_model_without_hosted_fallback`.
- Hosted/local live checks: **blocked** (no key/opt-in; Ollama not installed). Adapter
  contracts verified against local fakes only.

## Verification evidence (local, macOS arm64, 2026-10-14)

`make verify` → **10 passed, 0 failed, 0 skipped** (after Phase 3):

| Gate | Result |
|---|---|
| backend ruff lint + format | passed |
| backend mypy --strict (71 files) | passed |
| backend unit tests | 141 passed |
| frontend eslint (incl. jsx-a11y-x strict) + tsc | passed |
| frontend unit tests (vitest) | 7 passed |
| integration tests (real PostgreSQL roles, real Keycloak, real simulator HTTP) | 45 passed |
| browser tests (Playwright Chromium 156, real login, real worker) | 8 passed |
| frontend production build | passed |

Phase 2 exit gate evidence:
- 20 ordered / 18 received: `tests/integration/test_investigation_stack.py::test_twenty_ordered_eighteen_received_produces_cited_quantity_mismatch`
  (citations to PO JSON pointer, receipt CSV row and invoice PDF line, all `valid`) and
  `frontend/e2e/phase2.spec.ts` (citation opens highlighted source line).
- Missing/unsupported inputs → needs_information/manual_review: `test_missing_receipt_is_needs_information_without_asserting_non_delivery`,
  dev corpus cases fur-dev-012/013/015/019/020/021/024, it-dev-006/007/012.
- Unreachable source retried, not treated as missing: `test_unreachable_source_is_retried_not_treated_as_missing`.
- Worker restart resumes without re-running completed steps; stale worker cannot complete:
  `test_worker_restart_resumes_from_durable_steps`.
- Rules-only baseline on the seed corpus: `tests/evaluation/test_rules_baseline.py` (37 cases).

Phase 1 exit gate evidence:
- Logged-in user creates/reads own case: `tests/integration/test_api_cases.py::test_requester_creates_and_reads_own_case`, `frontend/e2e/phase1.spec.ts` (real login).
- Other tenant denied via API: `test_other_tenant_cannot_access_case_by_any_route`; via
  browser: `e2e/phase1.spec.ts::a user cannot open another organization's case`.
- Other tenant denied via database role: `tests/integration/test_rls.py` (12 tests incl.
  pooled-connection reuse across tenants and after an error).
- Restart preserves the case: `test_case_survives_application_restart` (engine disposed,
  new app instance).
- No paid API used.

## Blockers (affect only dependent work)

| Blocker | Blocks | Unblocked by |
|---|---|---|
| No Anthropic credentials / live opt-in | Live hosted-model checks | Owner provides key, purpose and spend ceiling |
| Ollama not installed | Live local-inference checks | Install Ollama and pull a pinned model |
| No GitHub sandbox repo/token/authorization for issue writes | Live connector run (OB-22) | Owner names a sandbox repo, provides a fine-grained token, authorizes writes |
| Tesseract not installed | OCR path for scanned PDFs | Install tesseract; otherwise scanned documents route to needs_review |
| No cloud account/authorization | Staging deployment | Owner authorizes and provides account |
| No operator participants | Pilot measurements | Pilot owner recruits participants |

## Known defects and limitations

- Rate limiting is per process and in memory; not a DDoS control.
- Browser tests create `INV-E2E-*` cases in the local demo database; there is no cleanup yet.
- The "Evaluation" navigation entry has no page yet (Phase 7).
- No genuine scanned paper document is in the fixtures; the scanned case is a synthetic
  image-only PDF. OCR is unverified (tesseract not installed) and routes to needs_review.
- Pack regexes for PDF/email extraction are template-specific; unseen supplier layouts will
  route to needs_review rather than parse.
- Retrieval is PostgreSQL lexical full-text search only; semantic retrieval not implemented.
- A run of a different model mode is refused (409) while another run is active, instead of
  silently returning the active run (found by a browser test; fixed in Phase 3).
- Development `.env` from the original Desktop location was uploaded to iCloud before
  eviction (ADR 0009); values are local-only development secrets.

## Next executable task

Phase 4: versioned proposals with canonical payload digests, approval binding (actor,
version, digest, policy, source manifest, expiry, separation of duties), transactional
operation + outbox, leased dispatch with attempt ledger, sim task connector with
idempotency/reconciliation capabilities, timeout-after-commit and duplicate-delivery tests,
review and execution-timeline UI.
