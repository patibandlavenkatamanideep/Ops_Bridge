# OpsBridge status

Last updated: 2026-10-15 · Branch: `feat/opsbridge-foundation` (pushed to
github.com/patibandlavenkatamanideep/Ops_Bridge; never pushed to `main`) ·
Working copy: `~/Projects/Ops_Bridge` (see docs/adr/0009).

## Summary by validation level

| Level | State |
|---|---|
| Local application complete | yes — Phases 0–9 done and verified locally (see docs/final-report.md) |
| Real model validated | not evaluated (no live opt-in, no credentials); benchmark used mocked responses only |
| External connector (GitHub sandbox) validated | not evaluated — contract-tested against a documented-API fake only; live run needs repo, token and authorization |
| Hosted CI | not run — workflow committed (PRs to main, manual dispatch), validated with actionlint only |
| Staging deployed | no — AWS plan documented in docs/deployment.md, not provisioned |
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

### Phase 4 — approvals, execution and recovery (exit gate passed locally)
- Versioned proposals with canonical payload digests; approvals bound to version, digest,
  policy, source manifest, expiry, scope and separation of duties (`services/proposals.py`).
- Approval consumption + operation + outbox event + dispatch job in one transaction.
- Executor with attempt ledger, read-back confirmation, reconciliation by declared connector
  capability, fencing-token guards, dispatch-time revalidation, cancellation and human review
  of unknown outcomes (`workflow/execution.py`, `connectors/tasks.py`).
- UI: Proposals & execution tab (create from validated draft, approve/reject the reviewed
  version, attempt timeline, cancellation, unknown-outcome review).
- Details, state machines and residual risks: docs/execution-and-recovery.md.
- Defects found and fixed while building this phase (evidence for the case study):
  1. Simulator route ordering captured `/orgs/{org}/tasks/{id}` with the generic records route;
     the executor's read-back check refused to confirm and marked `outcome_unknown` instead of
     claiming success. Fixed by registering task routes first.
  2. Setting `consumed_at` by UPDATE violated the insert-only grant on approvals; consumption is
     now written at insert time.
  3. Dispatch resolved a task connector before routing local actions; fixed.
  4. UI did not refresh the case after asynchronous operation completion; fixed.

Phase 4 exit gate evidence: `tests/integration/test_approvals_execution.py` (18 tests; see the
table in docs/execution-and-recovery.md) and `frontend/e2e/phase4.spec.ts` (investigator
proposes, a different approver approves, worker dispatches, read-back confirms, case stays
"Follow-up pending").

### Phase 5 — MCP services and sandbox adapter (exit gate passed locally; live GitHub blocked)
- Records MCP and Casework MCP on the official SDK (streamable HTTP), two-credential
  authorization (Keycloak service token + Ed25519 delegation token), server-side membership
  re-check, audited denials (`mcp_services/`).
- Executor can run through Casework MCP (`McpCaseworkConnector`); agent evidence search can
  run through Records MCP. The dev stack uses MCP transport; tests default to in-process and
  have dedicated MCP tests.
- GitHub Issues connector with declared capabilities (no idempotency, eventual lookup),
  repository allow-list and explicit write authorization; tier-gated per tenant.
- Details: docs/integrations.md.

Phase 5 exit gate evidence: `tests/integration/test_mcp.py` (16, real transport and Keycloak
tokens; permitted discovery/calls and denied calls), `tests/contracts/test_github_connector.py`
(10), `tests/integration/test_github_execution.py` (3). Browser tests (10) pass with the dev
stack in MCP mode; the dev audit trail shows `mcp.casework.create_follow_up_task` and
`get_follow_up_task`. Live GitHub run: **blocked** (no repo/token/authorization);
`tests/live/test_github_live.py` is skipped unless explicitly enabled.

### Phase 6 — privacy, retention and adversarial hardening (exit gate passed locally)
- Uploads (`POST /cases/{id}/documents`, `services/casework_admin.py`): kind allow-list,
  size cap enforced from `Content-Length` before buffering and chunked bodies refused (411),
  content sniffed against the declared type, safe basename, re-upload of a name = new version,
  same parsers/pack mappings as source records, audited.
- Case conversation (`/cases/{id}/messages`): author-only corrections via `supersedes_id`;
  deletion by author or tenant admin clears text, keeps who/when/why and digest.
- Tenant settings (`/settings`): default model mode, task connector and retention, gated by
  tier entitlements and by `tenant.manage_policy` / `tenant.manage_connectors`; read-only for
  auditors; members listed.
- Retention sweep (`workflow/retention.py`, job `retention_sweep`): deletes original bytes and
  their chunks, model/tool artifacts and message text past the configured days; keeps
  digests, findings and audit; API reports "deleted by retention policy". No schedule yet.
- Log redaction (`telemetry/logging.py`) on API, worker, simulator and MCP processes,
  including uvicorn access logs (OIDC `code`/`state`) and tracebacks.
- UI: Tenant settings page, Conversation tab, upload form on the Evidence tab.
- Docs: docs/privacy-and-retention.md, docs/security-and-threat-model.md (T1–T19).
- Defects found while building this phase:
  1. The dev access log printed full request URLs, so OIDC callback codes reached local
     process output (never committed). Fixed by the redacting filter; covered by a test that
     applies uvicorn's real logging config.
  2. Chunked request bodies bypassed the `Content-Length` limit check; now refused with 411.
  3. Connector changes were checked against `tenant.manage_policy` instead of
     `tenant.manage_connectors`; fixed.
  4. A retrieval isolation test first passed vacuously (query matched nothing); rewritten to
     prove the foreign term matches under its own tenant and returns nothing under another.

Phase 6 exit gate evidence: `tests/integration/test_hardening.py` (16): uploads (parse +
versioning; HTML disguised as text/PDF, bad image, unknown kind; size, traversal filename,
cross-tenant and role denial; chunked body 411; download no-store/attachment), conversation
correction/deletion, settings roles vs tiers, retention deletion with honest API state,
policy published before dispatch → `cancelled_before_dispatch` with no remote task, changed
PO → new version 2 and new manifest digest, retrieval isolation before ranking, artifact key
traversal/cross-tenant, pooled worker connection reuse across tenants;
`tests/privacy/test_log_redaction.py` (14); `frontend/e2e/phase6.spec.ts` (4).
Explicitly unverified with safe fallback: OCR (tesseract absent → `needs_review`), semantic
retrieval (not built; lexical only), live local inference (Ollama absent → blocked, no hosted
fallback).

### Phase 7 — evaluation, replay and EvalSeal (exit gate passed locally)
- Frozen benchmark `opsbridge-bench-v1` (commit 31515f0, before any held-out run): plan
  (docs/evaluation-plan.md), machine-readable acceptance policy, digest manifest of splits,
  document rendering, policies, prompts, scoring, modes and repeats. Corpus: dev 37,
  validation 20, held-out 40, security 7 (104; target ~120 not reached), hand-labelled,
  families and templates disjoint across splits; held-out includes an unseen invoice layout.
- Stack-level harness (`evaluation/bench.py`): each trial runs through simulator HTTP,
  services, worker and PostgreSQL (`opsbridge_eval`); rules_only and mock modes on the same
  cases; 3 fixed repeats.
- Strict replay (`evaluation/cassette.py`): keyed by case/mode/repeat/call with request
  digests; a missing or diverged call fails the run (never a fallback or live call);
  Python-level egress limited to loopback (`evaluation/egress.py`).
- EvalSeal 2.2.0 receipts (`evaluation/receipt.py`): OpsBridge as `LocalCallableTarget`,
  bound artifacts incl. `tool_acl`/`tool_responses` in EvalSeal's agent schemas, chained
  ledger signed with a local Ed25519 key; `eval-verify` fails on changed or missing files.
- Metrics (`evaluation/metrics.py`): case-majority, Wilson intervals, per-type
  precision/recall, safe vs unsafe misses, EvalSeal flip rate, paired McNemar, frozen gate.
- UI: Evaluation page (runs, comparability, uncertainty, gate, per-case verdicts for the
  user's own pack, receipt download); API `/api/v1/tenants/{t}/evaluations`.
- Worker survives a database outage with backoff (found when the DB container was
  recreated); idempotent `opsbridge_eval` database creation for existing volumes.
- **Held-out result (mocked responses):** next action 36/40 = 0.90 (Wilson 95% 0.77–0.96)
  in both modes; all 4 misses are the unseen-layout family and are safe escalations; 0
  unsafe misses; no discordant cases between modes. Gate: "pass (interval includes values
  below the target)". Real-model quality: not evaluated. Details: docs/evaluation-results.md.
- Defects found by validation/security runs and fixed before held-out: findings stating
  numbers missing from their values (rules 1.0.1), validator rejecting magnitudes and
  rule-title counts; harness ledger-linking bug.

Phase 7 exit gate evidence: edited bound artifact fails verification and a missing one
fails too (`tests/integration/test_evaluation.py::test_receipt_detects_edited_and_missing_artifacts_and_replay_miss_seals_nothing`);
missing/diverged replay response fails closed with nothing sealed (same file,
`test_strict_replay_reproduces_recorded_runs_and_fails_closed`); egress guard and manifest
drift (`tests/evaluation/test_offline_guards.py`); results state mode, units, splits,
settings, failures and uncertainty (docs/evaluation-results.md, results JSON);
`make eval-offline` reproduced identical held-out verdicts; 14 ledger records verified.

### Phase 8 — operations and staging preparation (exit gate passed locally; staging not deployed)
- Telemetry: OpenTelemetry spans from the API request (perimeter server span; client trace
  context not trusted; FastAPI native telemetry disabled) through the job (trace context in
  the payload), durable steps, model and tool calls and connector attempts, with case, run,
  operation, attempt, remote-reference and correlation ids; OTLP/HTTP or JSON-lines export
  when configured. Prometheus metrics with bounded labels on API `/metrics` (optional token)
  and worker `:9101/metrics`; queue gauges; tenant-scoped backlogs only via the aggregate
  `ops.metrics_snapshot()` (migration 0004, NOLOGIN owner role, column-level grants). Logs
  carry trace and correlation ids.
- Health: worker `/healthz` (loop heartbeat) and `/readyz` (loop + database); API `/readyz`.
- Alerts: 16 Prometheus rules with runbook links; `make alerts-test` (promtool, 9 cases);
  live drills with injected failures (IdP down, worker down) fired and resolved.
- Containers: one image (non-root, allow-list build context) for every role; compose
  profile `app` with bootstrap, health probes and volumes; clean launch from empty volumes
  and recovery drills (worker exit, database restart) passed; 12 browser tests passed
  against the containers.
- Backup/restore: `make backup`, `make restore-drill` (isolated database; counts, RLS,
  artifact digests, citations; tampering detected).
- Canary: `make eval-canary` against a pinned 64-case baseline (offline, egress-guarded).
- CI: `.github/workflows/ci.yml` (least privilege, pinned actions, no secrets) — not run.
- Docs: architecture.md, operations.md, deployment.md; threat model T20–T25.
- Defects found in this phase:
  1. The worker crashed when PostgreSQL restarted (fixed: backoff; re-verified in containers).
  2. An alert rule could never fire (free/total ratio with mismatched labels; caught by promtool).
  3. FastAPI 0.143 creates its own server spans that trust incoming `traceparent` and record
     raw URLs; disabled in favour of the perimeter span (found by a failing trace test).
  4. Podman re-splits exec-form health checks with spaces (`python -c "…"` became `import`);
     replaced by an `opsbridge probe` command.
  5. The MCP SDK's default DNS-rebinding allow-list rejected container hostnames; now an
     explicit allow-list derived from each service's URL.
  6. `verify-restore` with a relative path reported every artifact missing (uv changes the
     working directory); relative paths are now refused.
- Observed, not a defect of OpsBridge: the development laptop's data volume is ~90% full
  (23 GiB free); the disk alert went pending.

Phase 8 exit gate evidence: clean local launch (38 s from empty volumes, all health checks,
browser tests) and recovery drills (worker exit → restarted in 7 s; database restart → API
ready in 3 s, worker backed off without restarting; backup → isolated restore verified) —
docs/operations.md "Drills". Staging plan with resources, secrets, cost considerations and
rollback: docs/deployment.md. External deployment: not performed (no account/authorization).

### Phase 9 — portability and handoff (exit gate passed locally; pilot not run)
- Portability: `tests/integration/test_portability.py` runs the same core for both packs
  (investigation, citations, pack-specific routing and approvers, execution, cross-tenant
  denial, per-tenant roles for one user); `tests/portability/` proves no core package names
  an organization.
- Demo: docs/demo-script.md, with steps 4 (timeout reconciliation; unknown outcome recorded
  by a person) and 6 (IT local-only policy) automated in `frontend/e2e/demo.spec.ts`.
- UI: investigation shows model use and why a model was blocked; evidence table no longer
  squeezed at 1280 px; README screenshots from the running app (synthetic data).
- Docs: README, data-contracts, demo-script, operator-guide, pilot-plan,
  integration-onboarding, case-study, final-report; traceability 24 verified_local,
  3 implemented_unverified, 1 in_progress.
- Handoff checks: `make verify` 14/14; fresh clone from GitHub (doctor, setup, lint,
  typecheck, unit, canary, build, 18 receipts verify); offline replay at the final code
  reproduced held-out verdicts; documented make targets and CLI commands exist; claims review.
- Defects found in this phase:
  1. **Duplicate external effect**: with the task tracker no longer honouring idempotency
     keys and lookup unavailable, a resend after a timeout created a second task, because the
     simulator connector hard-coded its capabilities. Fixed by capability discovery (weakest
     contract when unknown); regression tests in-process and through MCP; recorded in
     docs/evaluation-results.md and docs/case-study.md.
  2. `make doctor` failed on a fresh checkout because `.env` did not exist yet (README order);
     now reported as the next step.
  3. The agents doc overstated what the validators guarantee; corrected.

Phase 9 exit gate evidence: the owner-facing material exists and the demo's recovery and
portability steps pass as browser tests; local completion is marked separately from live
model, sandbox, staging and pilot validation (docs/final-report.md).

## Verification evidence (local, macOS arm64, 2026-10-15)

`make verify` → **14 passed, 0 failed, 0 skipped** (final, after Phase 9):

| Gate | Result |
|---|---|
| backend ruff lint + format | passed |
| backend mypy --strict (102 files) | passed |
| backend unit + contract tests | 246 passed, 2 skipped (live GitHub test; one dev scenario with no reconcilable invoice in the finding-number invariant) |
| frontend eslint (incl. jsx-a11y-x strict) + tsc | passed |
| frontend unit tests (vitest) | 8 passed |
| evaluation canary (pinned baseline) | 64 cases, 0 changes |
| evaluation receipts (`eval-verify`: chain, signatures, bound artifacts) | 18 records ok |
| migration drift (`alembic check`) | passed |
| alert rules (promtool check + 9 unit cases) | passed |
| integration tests (real PostgreSQL roles, Keycloak, simulator HTTP, MCP transport, benchmark harness, telemetry, portability, quotas, retention schedule) | 120 passed |
| browser tests (Playwright, dev stack in MCP mode, incl. demo steps and axe accessibility scans) | 23 passed |
| frontend production build | passed |
| `make eval-offline` (run separately; appends replay records) | at commit 05081df: held-out and security replays identical to recordings; 18 receipts verified |
| fresh clone from GitHub (separate directory) | doctor, setup, lint, typecheck, test (243 + 8), eval-canary, build pass; receipts verify |

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
  Accumulated cases pushed INV-9001 off the first queue page and broke browser tests; the
  queue now has search (case/invoice/PO/title, wildcard-escaped) and tests use it.
- No genuine scanned paper document is in the fixtures; the scanned case is a synthetic
  image-only PDF. OCR is unverified (tesseract not installed) and routes to needs_review.
- Pack regexes for PDF/email extraction are template-specific; unseen supplier layouts will
  route to needs_review rather than parse.
- Retrieval is PostgreSQL lexical full-text search only; semantic retrieval not implemented.
- Retention runs daily per configured tenant and on request; backups are outside its reach. Extracted
  field values and canonical records are kept as decision inputs (docs/privacy-and-retention.md).
- Uploads are not malware-scanned; there is no in-app member editor (memberships are seeded).
- Browser tests add conversation notes and upload versions to the local demo case INV-9001.
- A run of a different model mode is refused (409) while another run is active, instead of
  silently returning the active run (found by a browser test; fixed in Phase 3).
- Development `.env` from the original Desktop location was uploaded to iCloud before
  eviction (ADR 0009); values are local-only development secrets.

- Benchmark: 104 cases (not ~120); labels authored by the same agent that wrote the rules;
  mocked responses only; held-out pass sits exactly at the 0.90 target with a 0.77 lower
  bound; supplier invoice layouts outside the pack mappings are escalated, not read.
- The egress guard covers Python sockets; libpq connects only to the configured local DB.
- Artifact storage is the local filesystem (EFS in the staging plan); no S3 store.
- Alert routing (pager/email) is not set up; quota values are starting points.
- Application processes must have synchronized clocks (NTP); comparisons against
  database-written timestamps use the database clock (found when the Podman VM clock drifted
  3 h 44 min after the laptop slept; fixed for quotas and retention).

## Next executable task

The locally achievable build is complete. Remaining work needs the owner (see "Blockers"):
run hosted CI (open a PR to `main` or dispatch the workflow); with authorization, the live
GitHub sandbox test and a recorded real-model benchmark; a cloud account for staging; a
pilot owner. Local improvements that need no authorization: an S3-compatible artifact store
for staging, and the next supplier invoice layout for the furniture pack (from approved real
samples, not the held-out layout).

### After-handoff hardening (2026-10-16)
- Per-organization quotas: investigations per hour (429 `quota_exceeded`, `Retry-After`) and
  model calls per day (recorded; rules fallback) — `test_quotas.py`.
- Database clock for quota windows and retention cut-offs — regression test with a skewed
  application clock.
- Delegation key from a secret (`OPSBRIDGE_DELEGATION_PRIVATE_KEY`); production never
  generates one — `tests/mcp/test_delegation_key.py`.
- Containers: read-only root filesystems, capabilities dropped, no-new-privileges; hardened
  clean launch passed 12 browser tests.
- Scheduled retention (daily per configured tenant via `ops.tenants_with_retention()`,
  migration 0005) — `test_retention_schedule.py`.
- Accessibility: axe-core WCAG 2.1 A/AA scans of every main screen, no serious or critical
  violations — `e2e/a11y.spec.ts`.
- Execution attempts keep their request and response as digest-matched artifacts.
- Traceability: 25 verified_local, 3 implemented_unverified (OB-01, OB-22, OB-25).
- `make verify` 14/14: unit 246 (+2 skipped), integration 120, browser 23.
