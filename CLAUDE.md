# OpsBridge — project instructions

## Mission and authoritative brief
- Build OpsBridge: an AI workbench for investigating procurement exceptions and executing approved follow-up actions.
- The business outcome is a correct, evidenced next action for a blocked invoice or purchase.
- Start with missing receipts, quantity mismatches, and price mismatches.
- Demonstrate portability through two purchasing contexts: office furniture and IT hardware.
- Keep policies, field mappings, tools, and evaluation cases configurable; avoid organization-specific branches in the core.
- Read OPSBRIDGE_BUILD_PROMPT.md completely before the first implementation.
- On later sessions, read this file, docs/STATUS.md, and the brief sections relevant to the current phase.
- These files specify a build; they are not evidence that features, integrations, deployments, or results already exist.

## Working agreement
- Respect platform instructions and the user's current explicit directions.
- Inspect existing files, repository instructions, git status, and test commands before editing.
- Preserve unrelated work. Never reset, clean, overwrite, or rewrite history to simplify your task.
- If empty, initialize a local repository and work on feat/opsbridge-foundation.
- Local implementation, tests, documentation, and reviewable commits of your changes are authorized by the build prompt.
- Continue through locally achievable phases after their gates pass; do not ask for permission after every phase.
- Credentials or infrastructure blockers affect only dependent work. Continue independent work and record the blocker.
- Paid model calls, external messages, writes to real customer systems, public deployment, pushes, merges, tags, and releases require specific authorization unless already granted.
- Complete the reviewable local work before asking for any such external action.
- Never bypass environment approvals, tool restrictions, or security controls.
- Maintain docs/STATUS.md with completed work, evidence, blockers, known defects, and the next executable task.
- Report commands and actual outcomes. A skipped test, mock connector, or unexecuted deployment is not a pass.

## Scope
- Deliver a local end-to-end application first, using synthetic data and deterministic mock model/tool adapters.
- Add a real MCP transport and one ticketing sandbox connector, with live checks conditional on credentials and authorization.
- Implement hosted and local model adapters behind the same contract; validate live behavior only when available.
- Keep the application usable without paid APIs after dependencies and container images are installed.
- No autonomous payments, invoice posting, vendor bank-detail changes, or purchase-order modification.
- No claims of fraud, regulatory compliance, production readiness, or business savings without supporting evidence.
- No generic workflow builder, Kubernetes platform, custom identity provider, model fine-tuning, or new cryptographic protocol in v1.

## Default structure and tools
- Backend: Python 3.12, FastAPI, Pydantic, SQLAlchemy, Alembic; uv for dependency locking.
- Frontend: React, TypeScript, Vite; npm with one committed lockfile.
- Storage: PostgreSQL, with pgvector when semantic retrieval is enabled.
- Background work: explicit durable state machine, PostgreSQL jobs/leases, and transactional outbox.
- Identity: standards-based OIDC; a local Keycloak profile for the development demo.
- Integrations: typed adapter interfaces and the official MCP SDK.
- Observability: OpenTelemetry plus optional Prometheus/Grafana deployment profile.
- Delivery: Docker Compose locally; a documented staging target and CI.
- Verify current upstream APIs and compatible supported versions before implementation; pin actual tested dependencies.
- Keep application logic in backend/src/opsbridge; keep frontend, fixtures, evals, infra, and docs separate.
- Add abstractions when needed by an implemented workflow; avoid empty services and speculative plugin systems.

## Data and decision invariants
- Money uses Decimal or integer minor units; never binary floating-point matching.
- Preserve currency, units, policy tolerances, rounding, and source-document versions explicitly.
- Unsupported currencies, conversions, units, or uncertain entity matches require review.
- Match against cumulative valid receipts and invoices without counting returns or reused quantities twice.
- Missing evidence means unknown or needs_information, not proof that goods never arrived.
- LLMs may extract, retrieve, summarize, and propose; explicit code checks arithmetic, permissions, and action preconditions.
- Every conclusion needs evidence references or an explicit uncertainty label.
- Validate citations against tenant, document version, page/line, and field access.
- Store concise decision explanations and supporting evidence, not private model chain-of-thought.
- Preserve original file digests separately from normalized-text digests; version normalization rules.

## Authorization and privacy
- Derive user and tenant context from a verified identity plus server-side membership.
- Never trust a model, header, browser field, or retrieved document to grant access.
- Keep service-tier entitlements separate from roles; a higher tier never grants another user's authority or data access.
- Apply tenant/resource checks before database reads, retrieval, cache access, downloads, and tool calls.
- Enforce PostgreSQL row-level security with non-bypass application roles; test workers and pooled connections.
- Scope machine identities separately from the human principal; record who requested and who executed an action.
- Validate OIDC tokens and sessions using maintained libraries; keep credentials server-side.
- Protect cookie-authenticated writes against CSRF; restrict CORS and apply rate limits.
- Treat uploaded documents, tool output, and emails as untrusted data, including embedded instructions.
- Use size/type limits, safe filenames, bounded parsing, URL allowlists, and sanitized rendering.
- Redact sensitive fields before remote model calls and telemetry. PII detection is not guaranteed anonymization.
- Use protected storage and managed encryption/key references in staging; never claim encryption that has not been configured.
- If policy requires local inference and it is unavailable, stop that inference path or request human review.
- Default tests and CI must not make paid model calls or contact customer services.

## Approval and execution invariants
- A proposal is an immutable, versioned action with a canonical payload digest.
- Approval binds actor, tenant, proposal version, payload digest, policy version, relevant source versions, and expiry.
- Recheck permissions, proposal state, expiry, and source versions before dispatch.
- An edit, policy change, revoked permission, or expired approval invalidates execution eligibility.
- Reject self-approval where tenant policy requires separation of duties.
- Persist the business operation, approval consumption, and outbox entry transactionally.
- Use one stable idempotency key per logical operation; attempts have separate identifiers.
- Delivery is at-least-once. Do not claim universal exactly-once execution.
- On timeout after possible remote commit, reconcile before any retry that could duplicate the effect.
- Without reliable idempotency or reconciliation, mark outcome_unknown and require review.
- Worker retries, leases, fencing, and concurrency controls must prevent a second independent authorized operation.
- Local cancellation cannot undo a dispatched external action; distinguish requested, confirmed, and unknown cancellation.
- Record read-back confirmation or explicitly mark an action unconfirmed.

## Trace and evidence contract
- Record tenant, case, workflow run, logical operation, step, attempt, and parent-attempt identifiers.
- Bind tool name/version, input and response digests, outcome, timestamps, and recorded/live/mocked origin.
- Retain protected originals where policy allows; hashes alone do not allow semantic review.
- Replay uses frozen responses with strict matching and network disabled; it never silently falls back to live tools.
- Record provider, requested/resolved model revision when available, supported sampling parameters, prompts, and code version.
- Distinguish raw configured values, provider defaults, and effective values actually known.
- EvalSeal is an adapter for evaluation evidence; use documented capabilities from a verified release.
- A valid signature or digest proves integrity under its stated trust assumptions, not input truth or hidden-run absence.
- Do not attribute a score change to judge drift or target behavior without a controlled experiment.

## Tests and quality gates
- Add focused tests for real risks: arithmetic, evidence, isolation, approvals, retries, parsing, and integration contracts.
- Include a real HTTP timeout-after-commit simulation, worker restart, duplicate delivery, and stale approval tests.
- Exercise both users and worker identities through real PostgreSQL and real OIDC in integration tests.
- Add browser tests for evidence inspection, approval/rejection, access denial, and failure recovery.
- Test a genuine MCP client/server exchange; internal Python calls alone are not MCP integration.
- Use independent fixtures/oracles, not only data produced by the parser or encoder being tested.
- Keep mock, recorded, and live results separately labeled.
- Benchmark rules-only and agent-assisted workflows on the same held-out cases.
- Freeze evaluation splits and settings before the final comparison; report failures and uncertainty.
- Never weaken a test or skip a failure simply to obtain green CI.
- Review generated diffs for secrets, unexpected network calls, unsafe defaults, and unsupported product claims.
- Preserve existing licenses and verify dependency terms; use synthetic or explicitly approved data in shared artifacts.

## Required developer command contract
- These are targets to implement and verify, not commands that already exist.
- make doctor: check prerequisites without printing secrets.
- make setup: install locked dependencies and prepare local config without overwriting existing secrets.
- make dev: start the local stack with health checks.
- make migrate: apply database migrations explicitly.
- make seed-demo: idempotently load only the designated synthetic demo tenants.
- make demo: start the stack, migrate, and seed non-destructively.
- make lint / make typecheck / make test / make test-integration / make test-e2e: focused reproducible gates.
- make eval-offline: replay the declared benchmark without paid calls or remote customer access.
- make verify: run the available local gates and clearly name missing prerequisites.
- make build: build backend and frontend artifacts from the tested dependency locks.
- make down: stop containers without deleting persistent data.
- Provide equivalent direct commands for environments without make.

## Delivery and evidence
- Keep this instruction file under 200 lines; place detailed procedures in docs.
- The detailed build prompt owns scope; docs/requirements-traceability.md tracks implementation and evidence.
- Update docs/STATUS.md and affected runbooks at every completed phase.
- Use small commits for coherent changes; include a meaningful test result with the commit summary.
- Never print credentials, raw customer documents, access tokens, or signed download URLs in progress reports.
- Distinguish implemented, locally verified, CI verified, sandbox verified, deployed, and pilot validated.
- At handoff provide startup commands, demo flow, acceptance results, limitations, and remaining external dependencies.
