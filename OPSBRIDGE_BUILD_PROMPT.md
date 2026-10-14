# OpsBridge — complete implementation brief and build prompt

Specification version: 1.0 · Prepared: 2026-10-14

Read this entire brief and CLAUDE.md before implementation. This is a requested build, not a description of existing software.

## 1. Your role and execution contract

Act as the senior implementation engineer for OpsBridge. Own discovery assumptions, system design, backend and frontend implementation, integrations, security, evaluations, operational documentation, and a reproducible demo.

The owner is Venkata Manideep Patibandla. The portfolio goal is to demonstrate Forward Deployed Engineering and Applied AI delivery: understanding an operational workflow, integrating data and tools, making sound trade-offs, deploying a usable system, and measuring outcomes.

Do the implementation. Do not finish with only a plan, screenshots, a scaffold, or mock dashboards.

Before editing:
1. Inspect the workspace, existing instructions, dependency files, git status, branch, and tests.
2. Preserve unrelated work. If this is an existing application, map its capabilities to this brief before proposing replacements.
3. If empty, initialize a local repository and feature branch. Otherwise use a feature branch without rewriting history.
4. Create docs/STATUS.md, docs/IMPLEMENTATION_PLAN.md, and docs/requirements-traceability.md.
5. Record initial assumptions and dependencies, then start the first executable vertical slice.

You may implement, run local tests, update documentation, and create small local commits containing your changes. Continue between phases when their gates pass. Do not repeatedly ask whether to proceed.

Do not push, merge, publish, tag, deploy publicly, spend money, send messages, or write to real customer systems without specific authorization already present in the session. Prepare those steps completely and make their effects reviewable first. Never bypass platform permissions or environment controls.

If credentials, Docker, package downloads, model access, or cloud infrastructure are unavailable, name the exact blocker, mark dependent verification as blocked, and continue independent work. Never relabel a mock as a verified integration.

Use primary documentation to confirm SDK methods, supported model parameters, protocols, and dependency versions. Pin the versions you actually test. If documentation is inaccessible, implement from locally verified interfaces where possible and mark remaining assumptions.

## 2. Product, user, and business outcome

OpsBridge investigates blocked procurement cases and helps authorized staff take the next appropriate action.

The first supported question is:

> Why is this invoice or purchase blocked, what evidence supports the explanation, and what should happen next?

The application connects purchase orders, invoices, receiving records, supplier messages, organizational policy, and task management. It produces an evidence-backed investigation and a reviewable follow-up action.

Primary users:
- Requester: submits a case and sees the records they are entitled to see.
- Investigator: reviews evidence, corrects extraction, and prepares a resolution proposal.
- Approver: approves or rejects permitted actions within their scope.
- Tenant administrator: manages membership, connector configuration, and policy versions.
- Auditor: inspects permitted history and receipts without acquiring execution privileges.

Administrative access does not automatically grant access to all document content or authority to approve every action. Model the permissions explicitly.

Define three distinct outcomes:
- Investigation completed: findings and missing information are documented.
- Follow-up completed: an authorized task or draft was created and confirmed.
- Business exception resolved: new evidence satisfies the actual reconciliation policy.

Creating a ticket must not mark an invoice discrepancy as resolved.

Procurement is the initial business process, applicable across industries. Two example organizations—office furniture and IT hardware purchasing—must use the same core with different source mappings and policies. This is evidence of portability within that workflow; do not claim support for arbitrary industries or unrelated workflows.

## 3. Scope and completion levels

### Required local release

Deliver:
- A complete UI, authenticated API, worker, database migrations, seeded synthetic data, and persistent case workflows.
- Three discrepancy types: missing receipt, quantity mismatch, price mismatch.
- Source extraction, evidence retrieval, deterministic matching, structured agent proposals, review, and approved follow-up actions.
- Real local OIDC authentication and two isolated tenants.
- Two real MCP services with a client/server integration test.
- A deterministic purchasing-system simulator with fault injection.
- A deterministic mock model adapter, a hosted model adapter, and a local inference adapter.
- Recorded tool replay with external network access blocked.
- EvalSeal integration through documented capabilities of a verified, pinned release.
- Tests, benchmark tooling, CI definitions, telemetry, and reproducible local deployment.
- A working second organization pack, operational runbooks, and FDE delivery documentation.

The mock model path proves execution and interfaces. It does not demonstrate the quality of a real language model.

### Credential-dependent validation

Implement a GitHub Issues sandbox connector as the first external task system. It must support only an explicitly configured demo repository. An issue is a follow-up work item; this is not an ERP integration.

Implement one hosted provider adapter using the official Anthropic SDK, with the model ID configured rather than guessed. Implement one local Ollama adapter using its documented API. Verify capability differences instead of assuming OpenAI-compatible behavior.

Contract tests and adapter code are locally achievable. Live provider tests, real sandbox writes, downloaded model execution, and production deployment depend on resources and authorization. Report their status separately.

### Outside the first release

- Executing payments, posting invoices to accounting, changing bank details, or editing purchase orders.
- Autonomous vendor contact: generate drafts; an externally sent message needs separate explicit authorization.
- Tax determination, currency conversion, fraud adjudication, or regulatory-compliance certification.
- A visual workflow builder, arbitrary code execution, autonomous browser actions, or unrestricted SQL generation.
- Custom cryptography, custom SSO, custom Kubernetes infrastructure, or fine-tuning.
- Building new timestamp/transparency-log infrastructure; consume verified EvalSeal functionality where available.
- Claiming integrations with SAP, Oracle, Dynamics, or other enterprise systems without implementing and testing them.

## 4. Requirement traceability

Copy this matrix into docs/requirements-traceability.md. Add implementation paths, test/evidence references, status, and unresolved limits. Requirement IDs must survive refactoring.

| ID | Requirement | Acceptance evidence |
|---|---|---|
| OB-01 | Scoped procurement problem and discovery assumptions | Workflow map, user roles, success criteria, assumptions distinguished from interviews |
| OB-02 | Three discrepancy types | Independent correct/incorrect/missing-data fixtures and UI flows |
| OB-03 | Cross-organization portability | Furniture and IT packs run without organization-specific core branches |
| OB-04 | Documents plus structured data | PDF/text/CSV/API evidence with stable provenance |
| OB-05 | Arithmetic and matching rules | Decimal, tolerance, cumulative-receipt, unit and currency tests |
| OB-06 | Evidence retrieval and citations | Authorized retrieval; citations resolve to the exact source version |
| OB-07 | Agent and specialist steps | Bounded workflow with explicit validation and escalation |
| OB-08 | MCP tools | Genuine MCP client/server requests; discovery and denied-call tests |
| OB-09 | Enterprise identity integration | Local OIDC login, sessions, expiry, roles, tiers, and machine identity tests |
| OB-10 | Tenant/resource isolation | DB, worker, retrieval, cache, artifact, and tool negative tests |
| OB-11 | Approval authority | Payload-bound approval, separation of duties, expiry and stale-input tests |
| OB-12 | Durable action execution | Outbox, leases, retries, crash recovery, and ambiguous-outcome tests |
| OB-13 | Attempt-level frozen responses | Every attempt correlated; replay never silently executes live |
| OB-14 | Privacy and local inference | Redaction tests, retention behavior, unavailable-local-model refusal |
| OB-15 | Stateful conversations | Case-scoped history, durable checkpoints, controlled correction and deletion |
| OB-16 | API and perimeter security | CSRF/CORS/rate-limit tests; documented proxy/WAF deployment boundary |
| OB-17 | Evaluation and uncertainty | Held-out comparison, per-case repeats, intervals, calibration limits |
| OB-18 | Provenance and EvalSeal | Actual receipt, fingerprint/config manifest, artifact tamper detection |
| OB-19 | Product observability | Correlated traces, cost/latency, queue/error metrics |
| OB-20 | Runtime monitoring | CPU/memory/disk and worker/DB health, meaningful alerts |
| OB-21 | Usable operator interface | Browser tests, keyboard navigation, empty/error/loading states |
| OB-22 | Sandbox adapter | Real HTTP contracts; live run only if explicitly authorized |
| OB-23 | Offline reproducibility | Fresh local setup and no-key replay; no hidden external calls |
| OB-24 | Deployment and recovery | Container smoke test, migration, backup/restore, rollback documentation |
| OB-25 | FDE handoff and outcomes | Demo, pilot plan, measured results with honest scope |
| OB-26 | Existing-project boundaries | EvalSeal adapter; no mandatory coupling to all prior repositories |
| OB-27 | Bounded compute and spending | Timeouts, quotas, run limits, explicit live opt-in and cost ceiling |
| OB-28 | Honest status and claims | Every claimed result points to evidence; blocked/live/recorded labels preserved |

## 5. Architecture and repository layout

Use a modular monolith with a separately runnable worker and narrowly scoped MCP services. Keep deployment simple enough for one engineer to explain and operate.

Default choices:
- Python 3.12, FastAPI, Pydantic, SQLAlchemy, Alembic, uv.
- React, TypeScript, Vite, npm, accessible UI components.
- PostgreSQL for application state, jobs, outbox, audit events, and document metadata.
- PostgreSQL full-text retrieval first; pgvector for enabled semantic retrieval.
- Private local artifact storage behind an interface with a configurable object-storage adapter.
- OIDC via a maintained client library and a local Keycloak container.
- Official MCP SDK; pin the implementation's tested protocol/SDK versions.
- OpenTelemetry; optional Prometheus/Grafana profile.
- Docker Compose for local execution and CI integration jobs.

Select compatible supported versions and commit lockfiles. Do not introduce Redis, a separate vector database, a workflow vendor, or an agent framework unless a measured requirement justifies it in an ADR. An external workflow framework is not needed to implement the initial bounded state machine.

Recommended layout, created incrementally:

~~~text
CLAUDE.md
OPSBRIDGE_BUILD_PROMPT.md
README.md
Makefile
.env.example
backend/
  pyproject.toml
  uv.lock
  src/opsbridge/
    api/             # HTTP, sessions, schemas, errors
    domain/          # deterministic matching and policies
    workflow/        # state machine, proposals, approvals, execution
    persistence/     # models, repositories, migrations
    evidence/        # ingestion, extraction, retrieval, citations
    agents/          # bounded steps and model adapters
    connectors/      # simulator and external task adapter
    privacy/         # redaction, data classification, retention
    audit/           # event and artifact manifests
    evaluation/      # evaluation harness and EvalSeal adapter
    telemetry/       # tracing and metrics
  tests/
frontend/
  package.json
  package-lock.json
  src/
integrations/mcp/
fixtures/
  furniture/
  it_hardware/
  adversarial/
evals/
  manifests/
  policies/
  results/
infra/
  compose/
  identity/
  observability/
  staging/
scripts/
docs/
  adr/
  STATUS.md
  IMPLEMENTATION_PLAN.md
  requirements-traceability.md
~~~

Use interfaces for source records, artifact storage, model generation, retrieval, and follow-up tasks. Keep organization field mappings and policy values in validated data files. Do not generate dozens of empty modules merely to match this tree.

## 6. Workflow and state model

The durable flow is:

~~~mermaid
flowchart TD
    A["Authenticated case intake"] --> B["Collect authorized evidence"]
    B --> C["Run matching and evidence checks"]
    C --> D{"Evidence sufficient?"}
    D -->|No| E["Request information or escalate"]
    E --> B
    D -->|Yes| F["Create versioned action proposal"]
    F --> G{"Approval and preconditions valid?"}
    G -->|No| H["Reject, expire, or revise"]
    H --> F
    G -->|Yes| I["Dispatch authorized operation"]
    I --> J{"Remote result confirmed?"}
    J -->|Yes| K["Record outcome and remaining case work"]
    J -->|Unknown| L["Reconcile or require review"]
    L --> K
~~~

Define separate state machines:
- Case: new, collecting_evidence, investigating, needs_information, awaiting_review, follow_up_pending, resolved, closed_without_resolution.
- Proposal: draft, awaiting_approval, approved, rejected, expired, superseded.
- Operation: pending_dispatch, executing, confirmed, failed, outcome_unknown, cancelled_before_dispatch.
- Attempt: started, succeeded, definite_failure, ambiguous_failure.

Document legal transitions and actor permissions. Do not overload one status field with all four meanings.

Persist each completed step with inputs, output references, workflow version, and timestamp. A restart resumes from durable state. Do not rerun an external write merely because the worker restarted.

Checkpoint LLM work separately from side effects. Retrieving fresh evidence can invalidate an existing proposal. Store a new investigation version instead of rewriting the explanation supporting an old approval.

## 7. Data contracts

Use validated schemas with schema_version, UTC timestamps, and server-assigned identifiers. JSON monetary values should be decimal strings with an explicit currency.

Minimum entities:
- Tenant, user membership, role, permission, service identity.
- Case, conversation message, investigation version.
- Supplier, purchase order, PO line, invoice, invoice line, goods receipt, receipt line.
- Source document, document version, extracted field, correction, evidence reference.
- Policy version and organization pack version.
- Proposal, approval decision, logical operation, execution attempt.
- Job, outbox event, audit event, artifact manifest.
- Evaluation case, evaluation run, per-case trial, evaluation receipt reference.

Every tenant-owned relation carries tenant_id. Use composite keys or equivalent constraints to stop a foreign key from linking entities in different tenants. The application and worker run without database-superuser or BYPASSRLS privileges.

Evidence references include source ID, immutable version/digest, page or row locator, extracted field, and access classification. Preserve original extraction and human correction with author, reason, and time.

Action proposal minimum fields:
- proposal_id, version, tenant_id, case_id, action_type.
- validated action parameters, human-readable effect, affected resources.
- evidence references and source versions, policy version, required permission.
- canonical action payload digest and canonicalization scheme.
- generated_at, expires_at, author/principal, status.

Approval minimum fields:
- approval_id, proposal_id/version, tenant_id, approver ID.
- approved payload digest, policy version, evidence/source-version manifest.
- decision, timestamp, expiry, scope, and separation-of-duties result.

Attempt minimum fields:
- tenant_id, case_id, run_id, step_id, operation_id, attempt_id, parent_attempt_id.
- tool and connector version, stable operation key, retry index.
- input/output artifact references and digests, response mode.
- started_at, finished_at, remote reference, outcome and error category.

Use one logical operation identifier across retries and a distinct attempt identifier for every invocation. Do not invent a response body when the network timed out; record the timeout and any later reconciliation evidence separately.

## 8. Deterministic procurement rules

Implement business rules as pure, testable functions independent of the LLM and HTTP layer.

Required matching behavior:
1. Resolve tenant, supplier, PO, line, invoice, and receipt identifiers before joining data.
2. Prefer exact identifiers. Fuzzy matches produce candidates requiring confirmation.
3. Check currencies and units. Unsupported conversion or ambiguous unit mapping requires review.
4. Use Decimal arithmetic with explicit rounding rules and policy-defined tolerances.
5. Track cumulative accepted receipts, returns, prior invoice allocations, and cancellations.
6. Do not reuse the same received quantity to justify multiple invoices.
7. Report each supported discrepancy independently; a case may have more than one.
8. Apply the policy version effective for the evaluated decision; retain it with the finding.

A quantity check can compare current invoiced quantity with allocatable received quantity after prior allocations. A price check compares the defined net unit-price basis with its PO counterpart. Document discounts, charges, and exclusions; do not improvise tax accounting.

For a price tolerance, explicitly configure absolute and/or percentage rules and whether both or either must pass. Handle zero-price denominators. Include exact-boundary and one-minor-unit-outside tests.

Missing receipt means the required record is unavailable. It does not establish that delivery did not occur.

The first release may route multi-currency, complicated service invoices, unsupported unit conversions, and ambiguous allocations to manual review. Report this as an explicit supported outcome rather than a parsing error or automatic match.

Do not classify supplier misconduct or fraud from a mismatch. Prevent duplicate case ingestion with stable source keys; advanced duplicate-invoice detection is outside the initial three discrepancy types.

## 9. Evidence ingestion, extraction, and retrieval

Support text-based PDF, plain-text supplier correspondence, CSV imports, and JSON source records first. Provide at least one genuine scanned-document fixture and an optional local OCR path. If OCR is unavailable, show unsupported/needs_review; never fabricate extracted text.

Requirements:
- Inspect content type, size, page count, and parser timeout; reject unsupported or oversized files predictably.
- Treat names and paths as untrusted; prevent traversal and cross-tenant artifact reads.
- Disable active content, external resource loading, and arbitrary URL fetching.
- Preserve raw bytes and raw SHA-256 digest before normalization.
- Store normalized-text digest and a named normalization version separately.
- Preserve page/row provenance through chunking and extraction.
- Validate extracted entities, dates, quantities, and monetary fields against typed schemas.
- Expose uncertainty and allow an authorized human correction.
- Never silently convert OCR guesses into authoritative business facts.

Retrieve within authorized sources before ranking. Post-filtering an unrestricted retrieval result is insufficient. Include tenant, role/resource scope, document version, and retrieval configuration in cache isolation.

Start with PostgreSQL lexical search for the no-model demo. Add semantic retrieval and hybrid ranking as a separately configured path with a pinned embedding model and measured retrieval tests. Do not label lexical search as hybrid retrieval.

For every decisive assertion, verify that cited evidence exists and supports the claim. A citation to a real page is not automatically evidence that the conclusion is true.

Include stale-policy and conflicting-document examples. Show which source controls the decision, and escalate when source precedence is undefined.

## 10. Agent behavior and model interfaces

Implement named steps: collect_context, retrieve_policy, reconcile_records, explain_findings, propose_follow_up, validate_proposal.

The reconciler is deterministic. Retrieval, explanation, and proposal generation may use a model. These roles need not be independent autonomous agents; add separate model calls only when evaluations justify them.

For every model step:
- Use bounded input, validated structured output, timeout, maximum output size, and an explicit failure path.
- Validate tool arguments and allowed action types before dispatch.
- Cap iterations and tool calls per run; prevent recursive agent spawning.
- Treat document and tool content as untrusted data.
- Never expose credentials, unrestricted database access, or shell tools to the model.
- Allow answer, clarify, propose, or escalate as explicit outcomes.
- Preserve useful partial findings when generation fails.

The mock model must be deterministic and explicitly marked mocked. It must not read hidden evaluation labels to generate the expected answer.

The Anthropic and Ollama adapters implement the same application-level interface, with separate capability declarations. Unsupported parameters must be rejected or explicitly declared unsupported; do not silently drop them.

Record requested model name, provider, endpoint identity without secrets, resolved revision if exposed, prompt-template digest, parameters, SDK version, and input/output token counts where available. If provider defaults or revisions are unknown, record unknown. Temperature zero is not a guarantee of reproducibility.

No live inference by default. A live run requires explicit operator opt-in, an authorized purpose, and a configured spend/call ceiling. Enforce concurrency and estimated-cost limits before dispatch where feasible; report provider usage separately from local estimates.

## 11. Identity, authorization, privacy, and perimeter controls

Use OIDC Authorization Code with PKCE and a backend-managed session through a maintained library. Delegate password/MFA/passkey support to the identity provider. Do not collect or implement enterprise passwords yourself.

Use secure HttpOnly cookies for browser sessions, explicit SameSite/CSRF handling, bounded session lifetime, logout, and server-side token storage. Verify issuer, audience, signature, expiry, state, and nonce according to the actual OIDC flow.

Use tenant membership stored or verified by the server. Browser-selected organization IDs are requests for a scope, not evidence of membership.

Keep service-tier entitlements separate from authorization. A tenant tier may enable an adapter, local inference, or a larger run budget, but it must not grant a viewer approval rights or cross-tenant visibility. Demonstrate two configured tiers without building subscriptions or billing.

Enforce tenant/resource authorization in:
- API handlers and service methods.
- PostgreSQL queries, RLS, and worker transaction context.
- Retrieval, caches, generated summaries, and artifact downloads.
- MCP tools and external connector targets.
- Approval, execution, reconciliation, and evaluation artifact export.

Test connection-pool reuse across tenants, worker sessions, and errors that might leave a transaction's tenant context behind.

Represent service identity and human principal separately. A broadly credentialed connector must still enforce the human principal's scoped business authority.

Implement PII redaction before hosted model calls and logs. Apply retention controls to raw documents, conversations, extracted text, embeddings, caches, and replay artifacts. Use opaque audit references and minimal event payloads so deletion can remove sensitive content without claiming that an audit hash is anonymized.

Sensitive-data routing is explicit: hosted permitted, local required, or human-only. If a required local model is unavailable, do not silently send data to a hosted service.

Use TLS for staging; safe local loopback defaults for development. Add API rate limits, request size limits, connector allowlists, restricted CORS, and telemetry redaction. Document the reverse-proxy/WAF configuration for the chosen staging environment. App rate limits alone are not a DDoS guarantee.

Manage secrets through environment/secret-store references; keep only placeholders in .env.example. Local demo credentials must be clearly labeled, development-only, and rejected by production configuration.

Use encrypted managed database/object-storage volumes and key references in staging where supported. Document who can access originals and backups, key rotation, and deletion behavior. Do not claim that local development files or a container volume are encrypted merely because the staging design includes encryption.

## 12. Approval, dispatch, and recovery

The model proposes actions. The backend checks authority and records approval. The executor can run only validated operations.

Supported initial actions:
- create_follow_up_task.
- assign_case, when the assignee is within authorized scope.
- create_supplier_message_draft; no external send in the default application.
- record_review_note.

Every external mutation requires the proposal/approval path in v1. Read-only investigation does not need artificial approval prompts.

At dispatch, revalidate the proposal version/digest, approver authority, expiry, policy version, and relevant source state. Approvals should use an explicit configurable lifetime; document the demo default as a product choice.

Do not hold a database transaction open during a slow external network call. Instead:
1. Lock/compare the proposal state and validate the authorization snapshot.
2. Create one logical operation and outbox entry while consuming the approval transactionally.
3. Let a leased worker dispatch the operation.
4. Record attempts and remote confirmations durably.

Mitigate changes between validation and remote mutation using conditional versions/ETags where available. If the connector cannot enforce a meaningful precondition, narrow the action or disclose the limit. Do not claim a local check eliminates every remote race.

Recheck revocation immediately before dispatch; use short-lived scoped credentials where possible. Describe the residual race once an external request is in flight.

Connector capability declarations must state:
- Whether the provider supports idempotency keys.
- Whether an operation can be reconciled by reference or search.
- Whether conditional writes are supported.
- Which errors mean definite failure versus an ambiguous result.

Retry definite retryable failures with bounded backoff and jitter. After a timeout that might follow a commit, reconcile first. A failed or eventually consistent lookup is not proof that the original write never happened.

For GitHub issue creation, do not assume native idempotency. A unique opaque marker can assist reconciliation, but an inconclusive search must not trigger an automatic duplicate create. Leave outcome_unknown and require investigation when certainty is unavailable.

Test concurrent workers, lease expiry, stale worker completion, duplicate queue delivery, crash before/after remote commit, and cancellation racing dispatch. Record cancellation_requested separately if an action is already in flight.

## 13. Tool integrations and MCP

Provide two services:

1. Records MCP: authorized PO, invoice, goods-receipt, document, and policy retrieval.
2. Casework MCP: approved follow-up task operations and read-back/reconciliation.

Suggested tool names are application contracts, not claims that a third-party SDK exposes them:
- get_purchase_order, get_invoice, get_receipt_lines.
- search_policy, get_evidence_excerpt.
- create_follow_up_task, get_follow_up_task, reconcile_operation.

Use tool schemas with bounded arguments and meaningful errors. Discovered tools must be filtered by permitted capability and workflow need; do not give every agent every tool.

Centralize policy evaluation in a shared service and enforce it at each relevant boundary. An internal caller is not automatically trusted. MCP services must validate their authenticated caller and the delegated authorization context.

For remote HTTP transport, follow the official protocol's authorization requirements, including audience/scopes and protected-resource behavior. Never pass a user's third-party access token through as an unrelated service credential.

Provide protocol-level discovery/call tests with actual MCP client/server transport. Ordinary function invocation tests do not satisfy this requirement.

The purchasing simulator must implement independent HTTP endpoints and persisted records with injection controls for delayed responses, definite failures, malformed records, and timeout-after-commit. Test controls must not be exposed in staging production mode.

The external GitHub adapter uses an allowlisted sandbox repository and least-privilege credentials. Only synthetic/redacted data belongs in the issue body. Read current official API documentation and record the endpoints and permission requirements in docs/integrations.md.

## 14. Audit, replay, and EvalSeal

Keep an append-oriented operational audit trail with server-assigned event sequence and protected artifact references. Do not claim that a database administrator cannot rewrite local history.

Bind each tool response to the exact attempt that received it. Preserve denied calls, errors, and timeouts alongside successful calls. Include frozen tool manifest/permission-policy versions and the tool-response artifacts.

Distinguish:
- workflow_run_id for one execution.
- evaluation_case_id and repeat_index for repeated experiments.
- logical operation_id and per-attempt IDs for side effects/retries.

Record raw and redacted artifact digests with explicit transformation metadata. If originals are withheld or deleted, show that semantic review/replay may be unavailable. A hash cannot reconstruct a document.

Offline replay:
- Runs without external customer/model traffic.
- Uses recorded responses keyed by tool identity, validated arguments, and occurrence/attempt context.
- Fails explicitly on an unmatched call or divergent path.
- Never silently substitutes a live call.
- Labels mocked and previously recorded responses separately.
- Reports which external state and model behavior were frozen.

Integrate EvalSeal through documented APIs or CLI of a verified release. Confirm available commands and receipt fields before implementation. Pin the package and retain an integration fixture.

Seal or bind the evaluation dataset, case IDs, policy, rubric, prompt templates, scorer configuration, model settings, tool manifests/responses, and code/environment manifest. If EvalSeal lacks a native field, attach an explicit versioned OpsBridge artifact through a documented interface; do not invent native fields or modify EvalSeal schemas.

Keep evaluation comparability separate from target intervention: changing the target model may be the comparison being tested. Changing the evaluator, dataset, tool environment, or scoring policy requires the appropriate non-comparable warning.

An evaluator fingerprint change demonstrates a declared configuration change. It does not identify every hidden provider change or establish the cause of a score difference. Attribute judge/model variance only with controlled repeated tests that hold the appropriate inputs fixed.

Receipt signatures require a trusted public-key reference to establish signer identity. Local signing or hashes do not establish independent time, input truth, or absence of hidden reruns. Use external timestamp functionality only when signature/trust validation is actually supported and tested; otherwise label that validation unavailable.

Do not implement a new RFC 3161 verifier in this project.

## 15. Operator UI and API behavior

Build these real screens, connected to backend data:
- Case queue: discrepancy type, owner, age, evidence readiness, and current state.
- Case detail: source records, side-by-side evidence, findings, uncertainty, and correction history.
- Proposal/review: proposed business effect, affected records, evidence, permissions, expiry, approve/reject controls.
- Execution timeline: every attempt, failure, reconciliation, and confirmed or unknown result.
- Evaluation view: case outcomes, repeated verdicts, comparability, uncertainty, cost, and downloadable permitted artifacts.
- Tenant settings: policies, members, connector health, retention, and model mode.

Use a restrained light interface with clear labels and accessible contrast. Include keyboard navigation, visible focus, loading, empty, denied, stale, and failed states. Do not add fictional KPI cards or success banners.

Expose model/trace implementation detail only where useful to investigators, evaluators, or operators. Keep the requester experience understandable.

API principles:
- Typed/versioned schemas and documented OpenAPI.
- Pagination for queues and event history; do not load all cases into the browser.
- Bounded document upload and long-running investigation endpoints returning job/run IDs.
- Idempotency support for user double-submits where appropriate.
- Version/precondition checks for edits and approvals.
- Consistent error bodies: code, safe message, correlation_id, retryability, and authorized details.
- Distinguish unauthenticated, forbidden, invalid input, conflict/stale version, unavailable dependency, and outcome_unknown.
- Stream sanitized progress or poll durable state; do not expose raw model reasoning.

Generate or validate frontend types against the backend contract. Test both browser behavior and API denial; disabled buttons are not authorization.

## 16. Evaluation plan and metrics

Create an evaluation plan before the final benchmark. Freeze datasets, splits, policies, model/settings, repeat schedule, scoring definitions, primary metrics, and budget.

Start with at least 30 development seed cases covering the three types and mixed/unknown outcomes. Grow the final synthetic corpus toward 120 distinct cases, with a proposed 60/20/40 development/validation/held-out split. Counts are design targets, not achieved results or proof of statistical power.

Split by scenario family and supplier/document template, not just random rows. Near-duplicate templates must not leak across splits. Hold out policy and schema variations where feasible. Independent expected findings and allowed actions must not be generated by the implementation under test.

Keep a separate security/reliability suite. Include missing data, contradictory messages, stale policy, partial delivery, prior allocations, unauthorized records, document injection, malformed files, and API failures.

Baselines:
- Deterministic matching plus templated guidance.
- Agent-assisted investigation using the same authorized inputs.
- Optional manually conducted operator trials, with actual participants and procedures documented.

Measure:
- Discrepancy precision/recall by type, with denominators and unknown/manual-review outcomes.
- Evidence correctness and citation support.
- Correct proposed next action and appropriate escalation.
- Unauthorized-action proposals versus actual unauthorized actions; report both separately.
- Approval-precondition enforcement and duplicate-side-effect prevention.
- Follow-up task completion separately from actual business-exception resolution.
- Time to a correct investigation or approved follow-up; operator time only when measured with people.
- Model/tool cost, tokens, model/connector latency, and end-to-end latency.

For repeated runs, retain the verdict sequence per case and define empirical instability consistently. If using non-majority count / total trials, name that definition and never silently replace it with adjacent-transition rate.

Use a fixed repeat schedule for the reported comparison or a predeclared valid sequential design. Development exploration can use small N, but do not repeatedly sample until significance and present ordinary fixed-N intervals.

Do not treat repeated trials of one case as independent new cases. Pair model comparisons by case; use case-clustered uncertainty for aggregate comparisons. Use binomial intervals only where the outcome, independence assumptions, and sample unit justify them.

An inconclusive result is acceptable. Zero observed failures is not zero risk. Different task sets or graders do not isolate a causal explanation.

Confidence scores must have empirical calibration evidence on held-out data before being treated as probabilities. Until then, use evidence readiness and validated rule status instead of a made-up percentage.

Mock-model benchmark results are engineering verification. A real model-quality claim requires a declared live or recorded real-model dataset. Record excluded and failed runs, costs, and environment.

Create a machine-readable demo acceptance policy before the final benchmark. Initial project targets are: all named security/recovery invariants pass; no observed unauthorized writes or duplicate external effects in the declared trial set; and at least 90% correct next-action decisions on the held-out case-level score. The 90% value is a prototype design target, not an industry standard or production safety guarantee. Record the exact metric, denominator, repeat aggregation, and uncertainty. Do not lower a frozen threshold after seeing results; report a failed or inconclusive quality gate and preserve the findings. If real model data is unavailable, mark the real-model quality gate not evaluated.

For operator pilot readiness, agree process-specific thresholds with the pilot owner and record them before the pilot. Keep this human validation gap explicit while completing the engineering work.

## 17. Tests, developer commands, and CI

Implement the command contract in CLAUDE.md and verify its real behavior. Include direct equivalents for environments without make. The Docker-first development path should work on Linux and macOS; document Windows/WSL support only as far as actually exercised.

Required layers:
- Pure unit/property tests for money, matching, canonicalization, permissions, and transitions.
- Integration tests against real PostgreSQL, worker jobs, OIDC, simulator HTTP, and MCP.
- Browser tests for the full investigation/review flow and negative authorization states.
- Contract fixtures for the task connector and model adapters.
- Offline replay/evaluation tests that fail if they attempt external egress.
- Migration tests, including upgrades from the previous fixture schema once versions exist.

Use pytest, ruff, and mypy for the backend; TypeScript checking, linting, unit tests, and Playwright for the frontend. Choose and pin maintained tools. Avoid superficial tests that only restate implementation.

Keep fixtures independent. Include at least one manually authored malformed document and one valid document from a different generation path. Do not validate a serializer solely against its own generated fixtures.

CI must run formatting/lint, type checks, unit tests, integration gates, relevant browser smoke tests, and build checks. Give integration services readiness checks. Isolate CI environment variables so behavior does not silently depend on GITHUB_ACTIONS or a developer's credentials.

Use least-privilege CI permissions, protect fork workflows from secrets, and never run untrusted pull-request code with privileged tokens. Pin action revisions where supported. Do not add deployment or release jobs that publish automatically without an intentional release policy.

Preserve existing licenses and check licenses for added dependencies, models, and fixtures. Use synthetic or explicitly approved data for the public demonstration; do not copy proprietary implementation code or customer documents into the repository.

Local passing checks and hosted CI results are different evidence. If CI has not run remotely, say so.

## 18. Implementation phases and exit gates

### Phase 0 — audit, contract, and discovery assumptions

Deliver the workspace inventory, requirement matrix, architecture sketch, scope, representative case, and risks. Write docs/discovery.md with intended users, current workflow assumptions, interview questions, baseline measures, and unverified assumptions.

Select dependencies from current primary documentation and record trade-offs in ADRs. Create the status tracker and plan.

Exit: the proposed first vertical slice, data boundary, and acceptance test are explicit. Proceed into code; missing interviews are a validation gap, not a reason to stop local implementation.

### Phase 1 — authenticated application skeleton and durable data

Deliver API, web shell, database migrations, local OIDC, tenant membership/RLS, case intake, simulator seed records, and case queue.

Exit: a logged-in user can create/read their case; another tenant cannot access it through the API or database role. Restarting the application preserves the case. No paid API is required.

### Phase 2 — complete deterministic investigation

Deliver ingestion, evidence references, the three matching rules, correction history, and an evidence-backed case detail screen.

Exit: the 20-ordered/18-received example produces the correct discrepancy and citations. Unsupported or missing inputs produce review/needs_information. Rules-only baseline runs on the seed corpus.

### Phase 3 — bounded agent assistance and model adapters

Deliver structured retrieval/explanation/proposal steps, mocked/hosted/local adapters, explicit capabilities, budgets, prompt registry, and source validation.

Exit: a mock-backed full flow works; malformed model output and injected document instructions cannot execute a tool or override rules. Hosted/local live checks are separately marked if blocked.

### Phase 4 — approvals, execution, and recovery

Deliver proposal versioning, approval binding, outbox/jobs, attempt ledger, reconciliation, and UI review/timeline.

Exit: stale or unauthorized approvals fail; restart and duplicate-message tests pass; timeout-after-commit produces one confirmed task or a clearly unresolved outcome, never a blind duplicate write.

### Phase 5 — genuine MCP and sandbox adapter

Deliver records/casework MCP services, protocol authorization, live transport tests, and the GitHub sandbox connector.

Exit: permitted MCP discovery/calls work and denied ones fail. The external adapter passes independent contract tests. A real sandbox run is required before labeling it live verified; do not block other phases if credentials are missing.

### Phase 6 — privacy, retrieval, and adversarial hardening

Complete redaction, retention, local-only routing, pooled-connection isolation, cache/artifact boundaries, upload defenses, semantic retrieval when enabled, and optional scanned-document handling.

Exit: the named adversarial tests pass. OCR, semantic retrieval, or local inference that could not be exercised remain explicitly unverified, with safe fallback behavior.

### Phase 7 — evaluation, replay, and EvalSeal

Deliver frozen manifests, case/attempt cassettes, strict offline replay, benchmark baselines, repeated-run analysis, and a real EvalSeal receipt with artifact verification.

Exit: an edited bound artifact fails verification; a missing replay response fails closed; evaluation results identify their mode, units, splits, settings, failures, and uncertainty.

### Phase 8 — operations and staging preparation

Deliver traces/metrics, service health, alerts, startup scripts, image builds, backup/restore exercise, migration/rollback notes, threat model, and deployment configuration.

Exit: a clean local launch and recovery drill pass. Stage a deployment plan with exact resources, secret requirements, cost considerations, and rollback. Deploy externally only when authorized; record hosted status honestly.

### Phase 9 — portability and FDE handoff

Deliver the IT hardware organization pack through schema mapping and policy configuration. Re-run relevant core and isolation tests without tenant-specific code branches.

Complete the demo script, operator guide, pilot plan, result report, integration onboarding guide, and requirement evidence matrix.

Exit: the owner can run the demo and explain the workflow, business rules, failure handling, evaluation, and limitations. Mark local completion separately from live provider, sandbox, staging, and pilot validation.

## 19. Required acceptance scenarios

Implement these as meaningful automated tests or explicitly identified operator exercises:

| Scenario | Required result |
|---|---|
| Order 20 chairs, receipt 18, invoice 20 | Quantity mismatch, cited records, no false claim of completed resolution |
| No receiving record | Needs information; do not assert goods were never delivered |
| Price exactly at tolerance / just outside | Deterministic documented boundary behavior |
| Receipt quantity already allocated to earlier invoice | No double allocation |
| Unknown unit/currency mapping | Manual review with the unsupported condition named |
| Two similar supplier names | No silent cross-supplier join |
| Conflicting supplier email and receiving record | Conflict shown, source precedence applied only if defined |
| Uploaded document says to ignore policy or call a tool | Treated as data; no authority change |
| User supplies another tenant's case/document ID | Denied without content leakage |
| Worker reuses connection after another tenant | Correct RLS scope; no inherited access |
| Proposal payload edited after approval | Approval cannot authorize the changed action |
| Policy/source version changes before dispatch | Revalidation rejects or creates a new proposal |
| Approver loses permission or approval expires | No new dispatch under that approval |
| Two concurrent workers receive the same operation | One logical operation; no duplicate authorization consumption |
| API commits task then drops response | Reconcile; no blind re-create |
| Provider has no idempotency and no reliable lookup | Outcome unknown and human review |
| Worker crashes before/after external commit | Durable recovery preserves operation and attempt history |
| Replay takes a different unmatched tool path | Explicit replay failure; no network fallback |
| Tool response/artifact is replaced | Bound artifact verification fails |
| Same target/settings, repeated outputs differ | Empirical variance reported with its sampling limits |
| Evaluator configuration changes | Non-comparable grading warning, not a claim of model regression |
| Sensitive input with required local model unavailable | No hosted fallback; clear blocked/manual path |
| Second organization uses different field names/policy | Adapter/config change, shared core unchanged |
| Task is created successfully | Follow-up complete; business case stays open until resolution evidence exists |

## 20. Operations, deployment, and observability

Correlate API request, case, workflow run, operation, attempt, and external reference. Keep sensitive payloads in protected artifact storage; telemetry contains safe identifiers and metrics.

Measure:
- Queue depth/age, lease expiry, retries, ambiguous outcomes, approval backlog.
- Model and tool request duration, errors, tokens, and actual/estimated cost.
- End-to-end investigation duration, human-wait duration, and execution duration separately.
- API and worker health, database pool saturation, CPU/memory/disk, and artifact storage use.

Avoid unbounded case/user IDs as metric labels. Use traces for high-cardinality investigation details.

Provide liveness/readiness endpoints that distinguish a live process from usable dependencies. Test alerts with injected failures; do not merely ship a screenshot of a dashboard.

Provide a fixed canary evaluation suite for regression checks against a pinned baseline. Offline CI can run it without spending; scheduled live canaries require explicit authorization, budget, and configuration. Record provider/model metadata and avoid attributing an observed change to a particular cause without controlled evidence.

Local deployment must use persistent named volumes, non-destructive seed commands, and readiness checks. make down must not remove data. Destructive demo reset needs a separate explicitly named command and a demo-only guard.

Prepare a supported staging topology: HTTPS ingress, UI/API/worker, private database and artifact storage, identity provider, scoped connector credentials, telemetry, backups, and resource limits. Prefer the owner's available cloud; if none is specified, document a small AWS container-based option and its alternatives without provisioning it.

An ECS/Fargate or equivalent runtime may need different process/resource assumptions from local Compose; document and test what is actually chosen. Do not present unexecuted infrastructure templates as a deployed service.

Write restore and rollback instructions. Test restoration using synthetic data in an isolated environment. Record recovery behavior and actual observations, not invented RTO/RPO guarantees.

## 21. FDE delivery artifacts and demo

Required documents:
- README.md: problem, screenshots from the running app, honest feature/status table, quickstart, limitations.
- docs/discovery.md: workflow, questions, assumptions, actual interview evidence if available.
- docs/architecture.md: boundaries, data flow, trust boundaries, and state machines.
- docs/data-contracts.md and docs/integrations.md.
- docs/security-and-threat-model.md and docs/privacy-and-retention.md.
- docs/evaluation-plan.md and docs/evaluation-results.md.
- docs/operations.md: diagnostics, retries, ambiguous outcomes, migrations, restore, rollback.
- docs/deployment.md: local and staging instructions with verification status.
- docs/pilot-plan.md: participant tasks, consent/data boundaries, measures, feedback, launch criteria.
- docs/demo-script.md: a concise demonstration with a successful investigation and a recovered failure.
- docs/requirements-traceability.md and docs/STATUS.md.

Suggested five-minute demo:
1. Show the blocked invoice and the human task being supported.
2. Inspect the matched records, missing quantity, cited message, and applicable policy.
3. Show a proposed action and the difference between investigator and approver access.
4. Inject timeout-after-commit; show reconciliation and the attempt history.
5. Show an edited artifact failing verification and the evaluation results with limitations.
6. Switch to the second organization pack to demonstrate policy/schema portability.

Measure the time needed to produce a correct investigation and approved follow-up against the declared baseline. Claim operator-time savings only from actual timed operator trials. Synthetic tests measure performance on the synthetic test set.

Publish real defects and fixes in an engineering case study. Include at least one failure that changed the implementation, with reproducible evidence.

No invented customers, testimonials, deployment scale, cost savings, production traffic, or successful security certification.

## 22. Do / do not checklist

Do:
- Deliver an authenticated vertical slice early and expand it through tested phases.
- Make uncertainty, unresolved actions, and unsupported inputs visible.
- Use explicit deterministic business and authorization rules.
- Preserve the exact tool context of retries and recorded experiments.
- Bind human approval to the operation the user actually reviewed.
- Validate outcomes through independent reads or mark them unconfirmed.
- Make local demos reproducible and label every model/tool mode.
- Preserve existing code and verify third-party APIs before integrating.
- Keep customer-specific behavior in validated packs and adapter mappings.
- Explain trade-offs and limitations in normal language.

Do not:
- Turn this into a general workflow-builder or an unrelated support-chat project.
- Implement every mind-map node as a separate LLM agent or microservice.
- Treat model confidence, memory, a document, or a tool response as authorization.
- Use an LLM for monetary arithmetic or silently accept uncertain extraction.
- Retry an ambiguous write blindly or promise exactly-once effects across arbitrary APIs.
- Treat a ticket creation as completed business resolution.
- Leak data through retrieval, vector search, caching, downloads, or telemetry.
- Claim SHA-256 hashes anonymize low-entropy private data.
- Fabricate API responses, provider revisions, model quality, benchmark wins, or production usage.
- Use synthetic fixtures from the same encoder as the sole integration evidence.
- Remove failing tests, relax policies, or skip negative cases just to show green CI.
- Commit real credentials or raw customer documents.
- Require all prior portfolio projects as dependencies.
- Finish after writing a plan when implementation remains locally possible.

## 23. Status, completion, and final report

Use statuses: not_started, in_progress, implemented_unverified, verified_local, verified_ci, verified_sandbox, deployed_staging, pilot_validated, blocked.

For every requirement, record the relevant status and evidence path/command. Some requirements can be verified locally while live validation remains blocked; show both.

A phase exit gate is a quality check, not a request for user approval. Continue local work after it passes. Ask only when a genuine decision, missing permission, or irrecoverable resource dependency prevents the affected action.

At each checkpoint update:
- Current branch and commits.
- Working behaviors added.
- Tests/commands run and exact outcomes.
- Failed or skipped checks and their reasons.
- Changed assumptions or architecture decisions.
- Known defects and next executable tasks.

Before final handoff:
1. Run the implemented local verification commands and inspect their outcomes.
2. Reproduce startup from a clean environment or clearly state the part that could not be tested.
3. Run the operator demo and inspect the real UI.
4. Review the diff for secrets, misleading claims, and missing requirements.
5. Ensure all documentation commands exist and use the pinned environment.
6. Return a requirement completion table, exact results, startup instructions, demo steps, and blockers.

The final report must distinguish: local application complete; real model validated; external connector validated; staging deployed; operator pilot measured. Do not collapse these into a single "production-ready" label.

## 24. Primary references to consult at implementation time

These are starting points; confirm the currently applicable version and record the actual interfaces used.

- Claude Code project instructions: https://code.claude.com/docs/en/memory
- MCP documentation and security guidance: https://modelcontextprotocol.io/docs
- FastAPI documentation: https://fastapi.tiangolo.com/
- PostgreSQL row security: https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- SQLAlchemy documentation: https://docs.sqlalchemy.org/
- Keycloak documentation: https://www.keycloak.org/documentation
- GitHub REST issues API: https://docs.github.com/en/rest/issues/issues
- Anthropic API documentation: https://docs.anthropic.com/
- Ollama documentation: https://docs.ollama.com/
- OpenTelemetry documentation: https://opentelemetry.io/docs/
- EvalSeal repository and released documentation: https://github.com/patibandlavenkatamanideep/evalseal
- Procurement matching reference: https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/three-way-matching-policies

These references support implementation and terminology. They are not evidence that OpsBridge is integrated with those vendors.

## Start now

Inspect the workspace, read the project instructions, create the Phase 0 artifacts, and implement the authenticated first vertical slice. Continue through locally achievable phases, recording evidence and isolating external blockers. Leave the owner with working software and an accurate handoff.
