# OpsBridge status

Last updated: 2026-10-22 · Branch: `feat/opsbridge-foundation` (pushed to
github.com/patibandlavenkatamanideep/Ops_Bridge; never pushed to `main`) ·
Working copy: `~/Projects/Ops_Bridge` (see docs/adr/0009).

## Summary by validation level

| Level | State |
|---|---|
| Local application complete | yes — Phases 0–9 done and verified locally (see docs/final-report.md) |
| Real model validated | not evaluated (no live opt-in, no credentials); benchmark used mocked responses only |
| External connector (GitHub sandbox) validated | not evaluated — contract-tested against a documented-API fake only; live run needs repo, token and authorization |
| Hosted CI | passing — PR #1 (first green run 39000811947 at 1f29197) and PR #2 were merged into `main` by the owner; later batches go to open PRs whose checks show the latest run; four jobs on ubuntu-24.04 since 2026-10-22 (the fourth builds, smoke-tests, inventories and scans the runtime image); runs on pushes to `main` too |
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
- Seed: `make seed-demo` idempotently creates only the two designated synthetic tenants
  (since 2026-10-20 it creates missing memberships and leaves existing ones as administrators set them).
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
| No genuine paper scan (only a clean raster) | Measuring OCR on real scans (skew, noise, stamps) | Owner provides approved scanned samples |
| No cloud account/authorization | Staging deployment | Owner authorizes and provides account |
| No operator participants | Pilot measurements | Pilot owner recruits participants |

## Known defects and limitations

- Rate limiting is shared across API instances (PostgreSQL) but is not a DDoS control.
- Browser tests create `INV-E2E-*` cases in the local demo database; there is no cleanup yet.
  Accumulated cases pushed INV-9001 off the first queue page and broke browser tests; the
  queue now has search (case/invoice/PO/title, wildcard-escaped) and tests use it.
- No genuine scanned paper document is in the fixtures. The OCR fixture is a clean raster made
  by macOS from a hand-written invoice; OCR quality was measured on that one page only.
  Tesseract is not installed on the development host (the real-engine tests skip there and
  run in the app container and in hosted CI).
- Pack regexes for PDF/email extraction are template-specific; unseen supplier layouts will
  route to needs_review rather than parse.
- Retrieval is PostgreSQL lexical full-text search only; semantic retrieval not implemented.
- Retention runs daily per configured tenant and on request; backups are outside its reach. Extracted
  field values and canonical records are kept as decision inputs (docs/privacy-and-retention.md).
- Uploads are not malware-scanned. People join by an invitation link the administrator
  delivers (OpsBridge sends no email); acceptance trusts the identity provider's email
  verification. Invitation addresses are cleared by retention only if the tenant sets
  `invitations_days`; users' own profile emails are not covered.
- Browser tests add conversation notes and upload versions to the local demo case INV-9001.
- A run of a different model mode is refused (409) while another run is active, instead of
  silently returning the active run (found by a browser test; fixed in Phase 3).
- Development `.env` from the original Desktop location was uploaded to iCloud before
  eviction (ADR 0009); values are local-only development secrets.

- Benchmark: 104 cases (not ~120); labels authored by the same agent that wrote the rules;
  mocked responses only; held-out pass sits exactly at the 0.90 target with a 0.77 lower
  bound; supplier invoice layouts outside the pack mappings are escalated, not read.
- The egress guard covers Python sockets; libpq connects only to the configured local DB.
- The S3 artifact store is tested against SeaweedFS only: server-side encryption, IAM and
  lifecycle behaviour on AWS S3 are unverified (docs/deployment.md).
- Alerts are routed (Alertmanager: page and ticket receivers) but locally reach only a sink;
  no real pager, queue or email is connected. Quota values are starting points.
- Application processes must have synchronized clocks (NTP); comparisons against
  database-written timestamps use the database clock (found when the Podman VM clock drifted
  3 h 44 min after the laptop slept; fixed for quotas and retention).

## Next executable task

The locally achievable build is complete. Remaining work needs the owner (see "Blockers"):
review and, if you choose, merge the open PR (#3 at the time of writing; merging is yours); with authorization, the live
GitHub sandbox test and a recorded real-model benchmark; a cloud account for staging; a
pilot owner. Local improvement that needs no authorization: the next supplier invoice
layout for the furniture pack (from approved real samples, not the held-out layout).

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
- S3-compatible artifact store (`S3ArtifactStore`, boto3 1.43.107), chosen when
  `OPSBRIDGE_ARTIFACT_S3_BUCKET` is set: same tenant-prefixed content-addressed keys, every
  read re-hashed, optional SSE-KMS request parameters, one cached client per endpoint. Uploads,
  downloads, worker and retention go through `artifact_store(settings)`.
  `verify-restore --artifact-bucket` checks a restored database against a bucket. Local
  SeaweedFS 4.47 in `make infra-up` (:8333) for tests.
  - Evidence: `tests/evidence/test_s3_store.py` (4: SSE/KMS and prefix passed through,
    content-addressed puts, tenant keys, tamper detection, restore check through either
    store); `tests/integration/test_s3_store.py` (3, SeaweedFS: round trip, cross-tenant key
    refused, overwritten object refused, upload and download through the API stored only in
    the bucket, retention deletes from the bucket, restore check reports a lost object);
    `make restore-drill` still ok (190 artifacts, 18/18 citations).
  - Found while testing: SeaweedFS's default 8 volume slots ran out after one or two buckets
    (InternalError on PutObject); compose now sizes slots from free disk and tests delete their
    buckets. SeaweedFS rejects SSE headers without its own KMS setup, so SSE is unit-tested
    only. With AWS, `s3:ListBucket` is required (otherwise a missing object is 403, not 404)
    and versioned buckets need a noncurrent-version lifecycle rule for retention.
  - `make verify` 14/14 (2026-10-16): unit 250 (+2 skipped), integration 123, browser 23.
  - `make image` rebuilt from the lock (28 s); an S3 round trip from that image with a
    read-only root, all capabilities dropped and uid 10001 succeeded against SeaweedFS. The
    full container stack was not relaunched with a bucket configured.
- Alert routing: Alertmanager v0.34.1 in the observability profile routes `severity=page` to
  a `page` receiver and everything else (including a rule without severity) to `ticket`, both
  with resolution notices; a local sink (`scripts/alert_sink.py`) stands in for the pager and
  the queue. `alert_drill.py --sink` waits for delivery, not just firing.
  - Evidence: `make alerts-test` (amtool config check and 6 expected routes; a deliberately
    wrong expectation fails the gate); `tests/contracts/test_alerting_config.py` (3: every rule's
    severity reaches its own receiver, resolution on, runbook sections exist). Live drill:
    Keycloak stopped → firing in Prometheus 145 s, delivered to `page` 155 s; restarted →
    resolution delivered 296 s; synthetic ticket alert delivered to `ticket` 30 s, resolution
    301 s.
  - Observed: resolutions arrive at the next group interval (5 min), documented in the runbook.
    The ticket route was drilled with an alert injected into Alertmanager, not one raised by
    Prometheus (no ticket condition could be produced safely in the dev data).
  - `make verify` 14/14 (2026-10-16): unit 253 (+2 skipped), integration 123, browser 23.

### Hosted CI (2026-10-17)
- PR #1 (`feat/opsbridge-foundation` → `main`) opened with the owner's authorization so the
  workflow runs; not merged. Workflow: `.github/workflows/ci.yml`, three jobs on
  ubuntu-24.04 with Docker, no secrets, no paid or external calls.
- Run 38999032200 (60af83d): **failed**. checks: `make lint` (import order in
  `mcp_services/client.py`); integration: passed (123); browser: 8 of 23 failed.
- Run 38999754931 (30864ee): **failed**. checks and integration passed; browser: 4 of 23
  failed.
- Run 39000811947 (1f29197): **passed**. checks: ruff, mypy (103 files), unit 253 passed and 2
  skipped, vitest 8, canary 64/64, 18 receipts, build; integration: promtool 16 rules,
  amtool config and routes, 123 passed in 117 s; browser: 23 passed in 1.5 min.
- Run 39001110996 (f2268a1, adds the MCP error description and its tests): **passed**. unit
  255 passed and 2 skipped, integration 123, browser 23.
- Defects the hosted runs found (details: docs/case-study.md, section 6):
  1. A stale ruff cache passed an import-order error locally (`tests/mcp` made the MCP SDK
     first-party). Fixed: `mcp` declared third-party; lint runs without the cache.
  2. On a fresh machine `make dev` started an empty simulator, so every investigation ended in
     needs-information; the README quickstart had the same gap. Fixed: the dev stack loads the
     dev corpus before starting the simulator.
  3. Browser tests depended on leftover data and could leak an injected simulator fault after
     a timeout. Fixed: tests create their own case; faults are cleared before each demo test
     and after the file. Reproduced on a fresh local database first (old test failed
     identically); after the fix 23/23 there twice and on the long-lived dev database.
  4. While diagnosing 3: MCP transport failures recorded only "ExceptionGroup … (2
     sub-exceptions)". Fixed: attempt records name the leaf errors
     (`tests/mcp/test_client_errors.py`).
- Traceability: 19 verified_ci (every listed test also passed in run 39000811947),
  6 verified_local (local-only drills, recorded benchmark, offline replay or review),
  3 implemented_unverified (OB-01, OB-22, OB-25).

### Optional local OCR (2026-10-19)
- Brief §9 asked for an optional local OCR path; `ocr_image` existed but nothing called it.
  Now, with `OPSBRIDGE_OCR_ENABLED=true` and tesseract present, image-only PDF pages are
  decoded in the PDF worker subprocess (40 MP cap, 4 images per page, `ocr_max_pages` 3) and
  read by tesseract (per-page timeout, one thread, validated language). The document stays
  `needs_review`; OCR text is stored as `ocr:` evidence with a notice in the UI; field values
  read from it are `ocr` / `needs_review` candidates, never records, so the rules never use
  them; corrections of them are refused (409 `ocr_candidate`). Off by default.
- App image: Debian tesseract 5.3.0 pinned (+about 90 MB). CI installs Ubuntu's tesseract and
  sets `OPSBRIDGE_REQUIRE_OCR=1`, so the OCR tests fail instead of skipping there.
- Fixture `fixtures/documents/rasterized_invoice.pdf`: the hand-written CUPS invoice rendered
  by macOS Quick Look at about 200 dpi, grayscale JPEG in a PDF (no text layer). Oracle: the
  hand-written text. Not a paper scan.
- Evidence:
  - Unit `tests/evidence/test_ocr.py` (9): off by default, missing tesseract reported, language
    validated, 1-bit image round trip, decompression bomb refused, labelled `ocr:` lines,
    page limit reported, candidates only.
  - Real engine `tests/evidence/test_ocr_tesseract.py`: token recall ≥ 0.9 (threshold set before
    measuring). In the app container, tesseract 5.3.0 read the page in 0.7 s with recall
    0.938 (45/48): `ST-150` was read as `ST-15@`, the date gained a period, `Line`/`Amount`
    became `pits`/`mount`. Passed in hosted CI with tesseract 5.3.4.
  - Integration `tests/integration/test_ocr_path.py`: a perfect OCR reading of INV-9001 (whose
    text-layer version is a cited quantity mismatch) still ends in human review, with OCR
    candidates stored and no invoice record; a correction is refused.
  - Browser `frontend/e2e/ocr.spec.ts` with real tesseract: passed against the hardened app
    containers (13/13 with phases 1, 2, 4, 6) and in hosted CI.
- Found by the new browser test on a fresh case: the upload confirmation vanished after the
  first upload into an empty case (the uploader was remounted). Fixed.
- `make verify` 14/14 locally (unit 264 + 3 skipped, integration 124, browser 23 + the OCR
  spec skipped without tesseract). Hosted CI run 39204032292 at ed1e99b: passed (unit 265 +
  2 skipped including the real OCR test, integration 124, browser 24 including the OCR spec).

### Acceptance-scenario audit (2026-10-19)
- `docs/acceptance-scenarios.md` maps each of the brief's 24 required scenarios (§19, wording
  copied) to the tests that check its required result; `tests/contracts/test_acceptance_matrix.py`
  fails if the table drifts from the brief or names a test that does not exist (checked by
  pointing it at a missing test: it failed).
- Gaps found and closed:
  1. Scenario 8 (instructions in an *uploaded* document) had no test; only the source-system
     email path did. Added `test_uploaded_document_instructions_are_data_not_authority`
     (compromised model; the uploaded copy is marked so the test proves it was retrieved).
  2. Scenario 21 (evaluator configuration changes): the frozen plan stored the scoring
     definitions but `verify` never compared them with the code. Now a changed scoring stops
     a benchmark run ("a grading change, not evidence of a model or rules change"), and the
     evaluation API/view marks runs from another plan or scoring as not comparable.
  3. Scenario 20 (repeated outputs differ): instability had no test and no sampling limit.
     The summary now reports repeats per case, a Wilson interval over flipping cases and
     what the repeat count cannot show; tested with synthetic disagreeing repeats.
- `make verify` 14/14 locally (unit 270 + 3 skipped, integration 125, browser 23 + OCR spec
  skipped without tesseract). Hosted CI run 39206261166 at 519a973: passed (unit 271 + 2
  skipped, integration 125, browser 24).

### Redaction corrupted identifiers (2026-10-19)
- Found by hosted CI run 39207047346 on a docs-only commit: the observability integration test
  failed because a case UUID ending in a 12-digit group was replaced by `[REDACTED:phone]` in
  a trace attribute. Re-running locally then hit the same class with a simulator task
  reference `T-1705292356`. The phone pattern matched digit runs right after a hyphen, and
  digit-only UUID groups; the old redactor altered 2,306 of 200,000 random UUIDs (1.15%; 2,277
  as phone, 30 as card numbers). It applies to logs, span attributes, hosted-model prompts and
  external task titles/bodies, so ids and long hyphenated business numbers could be lost there.
- Fix (`opsbridge-redact/2`): UUIDs are set aside before matching; no phone match right after a
  hyphen; span correlation ids skip the identifier patterns (credentials still removed). Bare
  long business numbers can still be redacted as phone numbers (documented).
- Evidence: `tests/privacy/test_log_redaction.py` (20,000 random UUIDs unchanged; ids kept,
  neighbouring phones/emails/cards still redacted) and `tests/privacy/test_trace_ids.py`; all
  four new tests fail on the old code. `make verify` 14/14 (unit 274 + 3 skipped, integration
  125, browser 23); observability test passed 6/6 repeats.
- Hosted CI run 39208207212 at b22f5c0 (the fix): passed, all three jobs.

### Outbound identifiers, benchmark replay, old Desktop copy (2026-10-19)
- Old working copy: the 32 uncommitted files in `~/Desktop/Projects/Ops_Bridge` were checked
  against the repository (30 superseded Phase 1 drafts; `perms.md` and `search.md` were saved
  GitHub documentation pages, now cited by URL in docs/integrations.md). With the owner's
  choice, they were zipped to `~/Projects/Ops_Bridge-desktop-leftovers-2026-10-14.zip` and the
  old copy removed (ADR 0009).
- External follow-up tasks lost a bare numeric PO number to redaction
  (`Confirm delivered quantity for [REDACTED:phone]`), and hosted-model prompts the same way.
  Outbound redaction now keeps the case's own identifiers exactly; personal data next to them
  is still redacted. Tests through the executor, the Casework MCP hop and the hosted-call path
  (a fake hosted provider records what would leave) fail on the old code; the hosted path had
  no wiring test before.
- Benchmark replay regression: `make eval-offline` no longer reproduced the held-out recording
  (agent mode 16/40 with 72 `QuotaExceeded` errors, rules 33/40 with 12) and still exited 0. The
  per-organization quotas of 2026-10-16 throttled the harness. Fixed: the harness switches
  quotas off (`quotas_enabled`); a replay fails when a verdict differs from its recording;
  `make eval-replay-check` (seals nothing) is a `make verify` gate and runs in hosted CI. The
  receipts of the failed replay came from uncommitted code and were not kept; the replay at
  e364dc0 reproduces held-out (36/40 both modes) and security (7/7) case by case; ledger 22
  records verified.
- `make verify` 15/15 (unit 278 + 3 skipped, integration 129, browser 23, replay check). Hosted
  CI run 39295328458 at 4becef9: passed, including the benchmark replay on Linux.

### Grafana dashboards (2026-10-19)
- `CLAUDE.md` and the brief name an optional Prometheus/Grafana profile; Grafana was missing.
  Added `grafana/grafana:13.2.3` to the observability profile: provisioned Prometheus data source
  and one file-based, read-only dashboard ("OpsBridge overview", 23 panels: service, work queue,
  cases/approvals/execution, connectors/tools/models, capacity; aggregates only, no tenant or
  case ids). Anonymous Viewer on localhost, login form and basic auth off, no reporting or
  update checks; an admin password is generated into `.env` by `make setup` anyway.
- Evidence: `tests/contracts/test_dashboards.py` (every metric and label used is exported;
  fails on a misspelled metric or label); `make dashboard-check` against the running stack:
  23 panel queries, 0 errors, 4 without data yet (no runnable jobs, no tool calls since restart,
  no tokens from the mock model, process memory not exported on macOS); rendered in a
  headless browser (docs/images/08-grafana-overview.png, synthetic local metrics).
- `make verify` 15/15 (unit 280 + 3 skipped, integration 129, browser 23, replay check).

### Tenant settings: policy versions and connector health (2026-10-19)
- Brief §15 lists policies, members, connector health, retention and model mode on the tenant
  settings screen; policies and connector health were missing.
- Policies: every policy version with its state (active = in force today by the database
  clock, using the same ordering as investigations; superseded; scheduled) and digest.
- Connector health (`GET /api/v1/tenants/{tenant}/connectors/health`, administrators and
  auditors): reachability and the capabilities the simulator advertises now (live discovery,
  3 s timeout; discovery failure is reported as "assumed weakest", not hidden), what they mean
  after an ambiguous create, attempts of the last 24 hours by phase and result, operations with
  an unknown outcome. For GitHub: configuration flags only; GitHub is not contacted.
- Evidence: `tests/integration/test_connector_health.py` (4: policy states; live capabilities,
  attempt counts after a confirmed operation, and the change when the tracker stops promising
  idempotency and lookup; unreachable tracker reported; investigators and other tenants
  refused); browser test in `e2e/phase6.spec.ts`; the accessibility scan covers the new sections.

### Shared API rate limit (2026-10-20)
- The limit was counted per process in memory, so two API instances (the staging plan runs
  two) allowed twice the configured rate and a restart reset it. Now one fixed window per
  session hash or client address is counted in PostgreSQL (`rate_limit_windows`, migration
  0006, RLS forced, API role only), aligned by the database clock, off the event loop; expired
  windows are pruned. If the database is unavailable, each process falls back to its own
  window, so a limit still applies. `rate_limit_shared` (on) and `rate_limit_window_seconds`.
- Evidence: `tests/integration/test_rate_limit_shared.py` (4): two app instances allow 3 of 6
  requests together (the old per-process mode, kept as a control, allowed all 6); the window
  rolls over after the stated `Retry-After`; a database outage still limits per process;
  expired windows are pruned. Cost measured locally: median 1.32 ms vs 0.55 ms per API request.

### Container demo database and restore-drill finding (2026-10-20)
- The restore drill failed: 4 attempt artifacts referenced by the dev database were missing
  from `var/artifacts`. Cause: `make demo-containers` used the same database as `make dev`,
  but the containers write artifacts to their own volume, so the container worker recorded
  rows pointing at files the dev store does not have. Repaired by copying the 4 files from
  the volume (sha256 checked); the drill then passed.
- Prevention: the containers now use their own database `opsbridge_demo` (created by the
  Postgres init script; `demo-containers` now runs `infra-up`, which rebuilds that image and
  reruns the script). Evidence: after the container stack ran browser specs on :8000, the dev
  database was unchanged (97 cases, 1022 audit events, 439 artifacts), `opsbridge_demo` had 3
  cases, 19 audit events and 12 artifacts, and all 12 artifact files were in the volume.
- Found on the way: phase3, phase4 and phase6 opened `INV-9001` and so passed only after
  phase2 had created it. On the fresh demo database phase4 failed. They now create-or-open
  the case; each spec passes alone against the containers (phase4 2, phase3 2, phase6 5).
- Operational note: when `infra-up` recreates the Postgres container (its image changed),
  Keycloak keeps dead pooled connections and the first sign-ins fail with an identity-provider
  error until the pool replaces them (about a minute here). Seen once; not fixed.
- Evidence: `make verify` 15/15 (unit 280 + 3 skipped, integration 137, browser 24); new
  backup at revision 0006 and `make restore-drill` ok (5598 rows, 0 tables without forced
  RLS, no artifact problems).

### Member management (2026-10-20)
- The brief gives tenant administrators membership management; memberships could only be
  seeded. Now `PATCH /api/v1/tenants/{tenant}/members/{id}` (and the Members section of
  tenant settings) changes another member's roles, approval scope or access (revoke,
  reinstate) with a required reason. Changes apply on the person's next request.
- Controls: `tenant.manage_members`; the tenant's membership rows are locked and the actor's
  own authority is re-read under the lock; nobody changes their own membership, so an active
  administrator always remains; unknown roles/action types refused; dropping the approver
  role clears the scope. The audit event holds before/after only; the free-text reason is on
  the membership row (`change_reason`, `updated_by`, migration 0007). The dispatch recheck of
  the approver now filters by tenant explicitly instead of relying only on RLS.
- Finding: `seed-demo` reset every existing demo membership to the fixture (roles, scope and
  status `active`), and the container bootstrap runs it on every start, so a revoked member
  would have regained access. Seeding now only creates missing memberships.
- Evidence: `tests/integration/test_member_management.py` (8, real PostgreSQL): a role change
  takes effect and is audited without the reason text; revoke and reinstate; a revoked
  approver's earlier approval is cancelled before dispatch and nothing reaches the tracker;
  validation and non-admin refusals; no cross-tenant change; an administrator demoted while
  their request is in flight is refused; two administrators demoting each other while both
  wait on the lock leave exactly one (with the re-check disabled both succeed and no
  administrator remains, and the in-flight request goes through, so both tests fail);
  re-seeding keeps in-app changes. Browser: `e2e/phase6.spec.ts` revokes and reinstates a
  member with reasons; the accessibility scan covers the open editor. `make verify` 15/15
  (unit 280 + 3 skipped, integration 145, browser 25); restore drill ok at revision 0007.
- Not built: protection against two colluding administrators, who can still grant each
  other any role. (Inviting new people followed; see "Membership invitations".)

### Membership invitations (2026-10-20)
- Administrators can now add people: `POST /api/v1/tenants/{tenant}/invitations` (email,
  roles, approval scope) returns a single-use link `/invite#<token>` once; open invitations
  are listed in tenant settings and can be withdrawn. The person signs in and accepts
  (`POST /api/v1/invitations/accept`). OpsBridge sends no email.
- Binding (chosen because a link can be forwarded): a 256-bit token, stored only as SHA-256,
  single use, 7-day expiry on the database clock, accepted only by a signed-in user whose
  identity provider verified the invited address (`email_verified`, now recorded at every
  sign-in), and only while the inviting administrator may still manage members. An active
  member cannot accept; a revoked member is reinstated with the invited roles. The tenant is
  found by token hash under its own RLS policy (read-only, only that row) and the change is
  made in the tenant's context with its memberships locked (migration 0008).
- The token travels in the URL fragment (not sent to servers, logs or Referer), is moved to
  sessionStorage before the first render so it survives sign-in, and is removed from the
  address bar. Pages outside an organization now have a header with Sign out.
- Keycloak imports the realm file only once, so `make identity-sync` adds users that were
  added to it later (`new-hire` verified, `unverified-hire` not; no memberships).
- Evidence: `tests/integration/test_invitations.py` (7): join with the invited roles
  (address normalized; single use; only the hash stored; token and address not audited); a
  forwarded link and an unverified address are refused; expired and withdrawn links fail;
  rules and non-admin refusals; an invitation lapses when its inviter is demoted; a revoked
  member rejoins with new roles; RLS outside a tenant shows only the matching row,
  read-only. Disabling the verified-address check or the inviter check makes a test fail.
  `test_oidc_login.py` drives real Keycloak: the unverified account is refused, the
  verified one joins. Browser: `e2e/invitations.spec.ts` (2, repeatable on the same
  database: invite, sign in from the link, accept, land in the queue; a forwarded link is
  refused); the accessibility scan covers the invitation form and page. `make verify` 15/15
  (unit 280 + 3 skipped, integration 153, browser 27); restore drill ok at revision 0008
  (row counts equal, every table with forced RLS).

### Retention for invitation addresses (2026-10-21)
- The invitations batch left a privacy gap: the invited address stayed on accepted, withdrawn
  and expired invitations indefinitely. A new retention key `invitations_days` clears it,
  counted from acceptance, withdrawal or (for an invitation never used) expiry. The row keeps
  roles, status, dates and token hash, plus `email_cleared_at` and the reason
  (`retention:invitations_days=N`); a cleared invitation leaves the tenant settings list. A
  pending invitation that has not expired keeps its address, since accepting needs it.
- The sweep runs as the worker role, which had no access to invitations. Migration 0009 gives
  it a tenant-scoped RLS policy, SELECT, and UPDATE on the three address columns only; a check
  constraint ties `email IS NULL` to `email_cleared_at`. (First draft of that constraint was
  inverted and was rejected by PostgreSQL against existing rows; fixed before anything ran.)
- Evidence: `test_invitations.py::test_retention_clears_the_address_only_after_an_invitation_is_closed_or_expired`
  (real PostgreSQL, sweep through the API and worker): with 30 days, addresses closed or
  expired 31 days ago are cleared, one withdrawn 29 days ago and an open one are kept; the
  open one can still be accepted; a second sweep clears nothing; the worker sees no
  invitations outside a tenant and cannot change their status. Breaking the sweep (ignoring
  expiry; dropping the cutoff) or the settings-list filter makes it fail. `make verify` 15/15
  (unit 280 + 3 skipped, integration 154, browser 27); backup and restore drill ok at
  revision 0009 (6305 rows, counts equal, every table with forced RLS).
- Not covered: users' own profile emails (`users.email`, refreshed at sign-in) and backups
  taken before a sweep.

### Agent tool calls replay from recordings (2026-10-21)
- Gap against brief §14: offline replay served model responses from the cassette but re-ran
  the agent's evidence search against the frozen corpus (OB-13 limit). Each replay produced
  a new response digest for the same call, and a replay failure inside the tool would have
  been recorded as an ordinary tool error.
- Now tool calls are recorded in `tool-cassette.json` beside the model cassette (keyed by
  case, mode, repeat and occurrence; checked against tool, version and argument digest;
  responses checked against their digests on load). Replay serves the recorded response
  without running the tool and labels it `recorded` (trial log, EvalSeal `tool_acl` and
  `tool_responses`); recorded denials and errors replay as such. Recorded hits are re-bound
  to the replay database's ids by document-version content digest. A missing, diverged or
  unbindable call fails the run: `ReplayFailure` now lives in `agents/contracts.py` and the
  agent step re-raises it. The tool cassette is bound in the sealed record.
- The held-out recording is never re-recorded, so its tool cassette (and the latest
  validation recording's) was written by `opsbridge.cli eval-backfill-tools` from the tool
  artifacts those runs kept under `var/eval/`; every entry matches the argument and response
  digests in the run's sealed trial log, and the command refuses otherwise. Without it,
  `make eval-replay-check` failed closed on `fur-heldout-222` (checked before the backfill).
- Evidence: `test_evaluation.py` (2 new): replay with the search patched to raise still
  reproduces the verdict and the recorded response digest, labelled `recorded`; a missing
  entry, other arguments, another tool version, and a hit for a document the case lacks each
  fail with `ReplayFailure`, produce no explanation and record no tool call; an edited
  response is refused on load; a recorded tool error replays as that error.
  `tests/evaluation/test_tool_cassette_backfill.py` (2). Removing the re-raise, executing
  the tool in replay, or skipping the re-binding each makes a test fail.
  `make eval-offline`: `heldout-replay-20261022T012711Z` and
  `security-replay-20261022T012742Z` reproduce case by case (held-out rules_only 36/40,
  mock 36/40; security 7/7, 7/7); `fur-heldout-222` shows `recorded` with the sealed
  digest `sha256:e4a49698…`; 26 records verify. `make verify` 15/15 (unit 282 + 3 skipped,
  integration 156, browser 27).

### External review, Phase A: core decisions (2026-10-22)
- An external review of `main` at `f0dbb1a` listed 18 findings; `docs/review-findings.md`
  tracks each one (status, regression tests, migrations, rollback, what is not covered).
  Phase A is done: F01–F04, F07, F11, F12. Each fix started with a test that failed on the
  previous source.
- F01: a retry after a definite failure, or after reconciliation proved an earlier write
  absent, created the task without rechecking cancellation, expiry, authority, edits or
  policy (10/10 combinations reproduced). Every new write now passes the full check;
  recovering a write that may have landed does not need it.
- F02: a local note or draft committed before the operation's confirmation; a crash in
  between duplicated it. Effect, attempt, outbox and confirmation now commit together, and a
  unique index allows one applied local action per operation (0010).
- F03: approvals did not cover where a task goes or the text that leaves OpsBridge; a
  settings change after drafting redirected the action. Destination and outgoing text are now
  in the approved payload, shown before approval, and rechecked at approval and before every
  write (0011).
- F04: a correction, upload or another case's new source version did not make the latest
  investigation stale, so its proposal could be approved, dispatched or the case resolved.
  Investigations now record their evidence state (0012); proposing, approving, dispatch (under
  the case lock) and resolving refuse a stale result. Resolution also checks the policy.
- F07: another supplier's invoice with the same number superseded this one and the case was
  matched against it; intake merged INV/123, INV.123 and INV123; a reused idempotency key
  returned the first case for any request. Invoices are now current per supplier (0013), a
  case may name its supplier, an unnamed one stops for review when the number is ambiguous,
  look-alike numbers need confirmation, and replays are recognised by key and request (0014).
- F11: corrections to fields matching ignores (currency, dates, units…) were accepted; now
  refused, and the UI offers Correct only for the seven fields matching applies.
- F12: an unreadable replacement (truncated PDF, image-only PDF, unsupported type) left the
  previous extraction current. Earlier records are now superseded, and a refused current
  document sends the run to manual review.
- Evidence: `make verify` 15/15 (unit 282 + 3 skipped, integration 186, browser 27); benchmark
  replay reproduces held-out and security; canary 64/64; backup and restore drill ok at 0014
  (7083 rows, counts equal, every table with forced RLS). The browser suite caught one fault
  in a first F07 version (see review-findings.md); fixed before this entry.
- Next: Phase B (F05, F06, F08, F09, F10, F16).

### External review, Phase B: concurrency and failure (2026-10-22)
- F05: an investigation handler never saw its job, so a step outlasting the lease let a
  second worker run the same investigation, a worker that had lost the job still wrote its
  results, a job whose worker died on every attempt was re-claimed forever, and a resumed run
  started a fresh model-call budget. Investigations now hold their job's lease: writes are
  fenced, the lease is renewed during a step for at most the step limit (300 s), finalization
  decides under the case lock, exhausted jobs are given up, and the budget counts recorded
  calls.
- F06: spend and the daily call quota were checked, then the model called; concurrent workers
  overspent. A call is now reserved (pending row with its estimated cost) under the
  organization's lock before it is sent and settled afterwards; crashes and timeouts keep the
  estimate charged; investigation starts are serialized per organization for the hourly quota;
  prices and the ceiling must be finite and non-negative.
- F08: retention deleted bytes another artifact still used, before the database committed. It
  now queues the keys with the row changes (migration 0015) and deletes bytes only when no
  live artifact references them, under a per-content lock that writers also take.
- F09: any session cookie, verified or not, chose its own rate-limit bucket. Every API request
  now counts against its client address (X-Forwarded-For only from `trusted_proxies`); a
  verified user also has their own budget; the fallback limiter is truly bounded.
- F10: unit inverses are derived exactly (1/6 is refused, not rounded); policies refuse zero,
  negative or non-finite factors, contradictory pairs and aliases naming two units.
- F16: local artifact reads verify the digest; an altered original is refused, not served.
- Each fix started with a test that failed on the previous source (F05's resumed-budget case
  was found by reading: the test stack itself needs the new worker interface).
- Evidence: `make verify` 15/15 (unit 310 + 3 skipped, integration 199, browser 27); benchmark
  replay reproduces held-out and security; canary 64/64; backup and restore drill ok at 0015
  (7465 rows, counts equal, every table with forced RLS).
- Next: Phase C (F13 production profile, F14 release qualification in CI, F17 strict verify).

### External review, Phase C: deployable artifact (2026-10-22)
- F13: `staging` (synthetic data; the purchasing simulator is allowed) and `production` now
  refuse the local artifact store, unencrypted S3, disabled quotas, a per-process rate limit
  and missing trusted proxies; `production` also refuses demo users and, since no production
  purchasing adapter exists, refuses to start at all. Settings checks only.
- F14 (the part inside the repository): CI runs on every push to `main` as well as PRs (main
  runs are never cancelled), fails when the regenerated API types differ from the committed
  ones, and builds the runtime image from the locks with base images pinned by digest,
  smoke-tests it (uid 10001, CLI, /healthz), uploads an SPDX SBOM and fails on fixable
  critical vulnerabilities. A Trivy scan of the image found fixable advisories only in Pillow
  12.3.0 and certifi 2026.7.22; both were updated (13.0.0, 2026.10.19; days-old fix
  releases). Dependabot proposes weekly updates. Not done (owner's): protecting `main`,
  publishing, signing and provenance of images.
- F17: `make verify` lists every gate every time (skipped ones with the missing prerequisite,
  and tests skipped inside a gate); `make verify-strict` is the release gate and fails on any
  skip. Here it fails as intended (tesseract not installed: one unit and one browser OCR
  test skip). A scenario test that skipped not-applicable cases now leaves them out of its
  parameters; live tests are excluded from the unit gate by marker.
- Hosted CI run 39848032134 at e6f8702: all four jobs passed (unit 328 passed, 0 skipped
  with tesseract; the image scan covered 83 targets with no fixable critical finding).
- Found on the way: the host disk was 99% full (127 MB free) during a local image rebuild,
  which failed with corrupted blobs. The images, scan files and replay-check work
  directories this work created were removed (2.3 GB free afterwards); other projects'
  images were left alone. The rebuilt image was therefore scanned only in CI. Later the
  shared Podman VM could not start any new container (writeback errors on its disk while the
  host was full; overlayfs then refused mounts); running containers were unaffected. Cleared
  without restarting the VM by `sync -f` on its container storage (the file that lost writes
  was the failed build's deleted temp file); the alert-rule gate and `make verify` pass again.
  Before building images locally, check that several GB are free.
- Next: Package 11's local part (a source-contract harness), F15 (claim meaning in model
  validation) and F18 (operator UX); integration with a real purchasing system and the pilot
  need the owner (docs/review-findings.md).

### External review: F15 and F18 (2026-10-22)
- F15: model explanations and drafts could state real numbers in the wrong roles ("bills 18
  while 20 were received") or units and still pass. A number next to a role word must now
  equal a finding value of that role, and a number followed by a unit must use the findings'
  unit. The check accepts every finding summary in the dev and validation corpora (a first
  version flagged two; fixed), and none of the 120 recorded held-out mock explanations is
  newly rejected. Comparisons, dates and prose without numbers are still unchecked;
  structured claims would need a new prompt and benchmark version.
- F18: the server now states why each viewer cannot decide a proposal (expired, no
  permission, out of scope, separation of duties under this policy); the card notices expiry
  while open; queue search is debounced; the README screenshots no longer show a loading
  page. Saved views, due dates, a split settings page and operator sessions are not done.
- Evidence: integration 200, browser 27 (+1 OCR skip here), agents 93, unit 329; benchmark
  replay reproduces held-out and security.
- What remains needs the owner: a real purchasing system and task destination (Package 11),
  independently labelled data and a real-model comparison (Package 12), the pilot, branch
  protection and release publishing (docs/review-findings.md).

### Purchasing-source contract, Package 11 local part (2026-10-25)
- No customer purchasing system or export has been chosen, so, as the review asked, this
  builds the contract and its harness and records the blocker; nothing is live-validated.
- Fixed, each with a regression test that failed before (docs/review-findings.md):
  - a listing read only its first page, so later receipts were silently missing (6 of 10
    received counted as a discrepancy);
  - a receipt deleted, voided or moved to another PO in the source kept counting as received;
    it is now marked "No longer in the purchasing system" and stops being current, and comes
    back if the source holds it again (migration 0016);
  - nothing recorded when the source was read: a result may now be acted on for 24 hours
    (`source_max_age_hours`), the case shows the read time and an "Out of date" reason;
  - a listing longer than the connector reads goes to manual review instead of concluding;
  - the policy date came from any supplier's invoice with the number, and a case with no
    single invoice date could never get a proposal once a future-dated policy existed.
- `connectors/source_contract.py` (`purchasing-source/1`) states what an adapter must do;
  `tests/contracts/test_purchasing_source_contract.py` (21) is the suite a real adapter must
  pass against its sandbox. docs/integration-onboarding.md §4 describes both.
- Not done: tenant-bound task connections; a background sync or change feed (reads stay per
  case); saved views and due dates (F18).
- Evidence: `make verify` 15/15 (unit 350 incl. the 21 contract checks, integration 208,
  browser 28); benchmark replay reproduces held-out and security; 0016 downgrade and upgrade
  on the test database.

### Dependabot follow-ups and tenant task connections (2026-10-25)
- Dependabot: #5 (Python 3.15 image) closed by Dependabot after the ignore rule; #11
  (TypeScript 7) told to ignore that major. PR #16 unblocks #8 (axe-core 4.14 found citation
  buttons whose accessible name did not contain their visible text; fixed) and replaces #14
  (anthropic 2.1.0: the key goes as a bearer token; the SDK's own tracing is kept to metadata
  and sends no trace context). Merging is the owner's: this session may not merge.
- Task connections (Package 11): each tenant has a versioned connection (connector,
  destination, credential reference, reason, who) instead of deployment-wide GitHub settings
  (0017). Repositories must be allowlisted; rotate, move and revoke from tenant settings.
  Approvals bind the destination; dispatch writes and recovers at the approved destination.
  A probe on the previous code showed recovery looking in the newly configured repository and
  leaving a created issue as `outcome_unknown`.

### No resend while a create may still land (2026-10-25)
- After a create timed out, a reliable lookup that found nothing was taken as proof that it
  had not happened, and the task was sent again. A tracker can still commit the first request
  after the client gave up (or the Casework MCP hop can deliver it late), so a tracker without
  idempotency keys got the task twice; reproduced with a create committed 6 s after the
  request.
- Creates now carry a request deadline (recorded on the attempt, migration 0018, passed
  through the MCP hop). Finding nothing proves absence only once the deadline plus a clock
  allowance has passed and the tracker honours deadlines (the simulator refuses late
  creates); before that the job looks again later; a tracker without deadlines goes to review.

### Case ownership and queue views (2026-10-25)
- Nothing could make a person a case's owner; the queue's owner column was always empty.
  Investigators can now take, hand over and release cases; a change made on a stale view is
  refused rather than overwriting someone else's claim; closed cases keep their owner; every
  change is audited.
- The queue has views (Mine, Unassigned, Awaiting approval, Needs review) with counts, an
  oldest-first order, flags for proposals awaiting a decision and operations needing a human
  check, and keeps its state in the URL. Needs review is the recovery view for operations
  whose outcome is unknown.
- Not done: due dates (no payment terms in the source data), per-person saved views.

### Parser bounds: nested JSON and PDF memory (2026-10-26, review Package 9)
- JSON nested about a thousand levels deep made the decoder raise RecursionError: an upload or
  source record failed with an unexpected error, a source listing escaped the connector's
  error handling, a model answer failed its step instead of being rejected. Nesting is now
  counted before decoding wherever outside JSON is read (`safejson`). API request bodies were
  already refused with 400.
- A PDF under 1 MB that inflates to text used about 850 MB in the parser until the timeout.
  The parser process is capped at 256 MB on Linux (verified in the app image: the bomb is
  rejected in 2.2 s, a 50-page PDF with 3.9 MB of text still parses) and at most two PDFs are
  parsed at once per process. macOS does not enforce the cap (development only).

### Model claims: comparisons and dates (2026-10-26, F15)
- Model text could reverse a comparison ("receipts total 18 and exceed the invoiced 20"),
  state a wrong difference direction ("20 that is 2 below the 18 received") or invent a date
  ("received on 2026-10-15") and still pass validation. Comparisons between stated numbers
  must now hold, and every date must appear in the cited evidence, findings, case or policy.
- Found on the way: true difference phrasing ("bills 2 more than the 18 received") and a real
  date were rejected before; both are accepted now.
- No false alarms: every dev and validation finding summary passes, and 0 of the 120 recorded
  held-out mock explanations are rejected.

### API responses checked in the browser (2026-10-26, F18)
- Pages trusted that the server's JSON had the shape the generated types describe. After a
  deployment where page and server versions differ, a missing field would render as empty;
  on an approval screen that means approving what was not shown. Every typed response is now
  checked against its OpenAPI schema (a generated, slimmed copy: under 5 KB gzipped); a
  mismatch shows "the app and server versions differ; reload" instead.

### Negative quantities and prices (2026-10-26)
- Correcting INV-9001's invoiced quantity to -20 made the case match (the 18 received
  covered it), which is what resolving the case needs; a source invoice with negative lines
  (a credit note) matched the same way. Matching now sends any negative quantity or price to
  manual review (`negative_value`, rules 1.1.0), and corrections refuse negative values.
- Canary: no case changed, only the rules version; the baseline is updated in its own commit.
