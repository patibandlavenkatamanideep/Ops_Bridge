# Architecture

OpsBridge is a modular monolith (ADR 0001): one Python package (`backend/src/opsbridge`)
run as several processes, a React UI, PostgreSQL and an OIDC provider. This page shows the
boundaries, the two main data flows, the trust boundaries and the state machines. Details
live in the linked documents.

## Processes

| Process | Command | Role | Database role |
|---|---|---|---|
| API (+ built UI in containers) | `opsbridge api` | Sessions (OIDC), case/evidence/proposal APIs, uploads, settings, evaluation view, `/metrics` | `opsbridge_app` |
| Worker | `opsbridge worker` | Leased jobs: investigations, dispatch, retention; `/healthz` `/readyz` `/metrics` on :9101 | `opsbridge_worker` |
| Records MCP | `opsbridge mcp-records` | Read-only evidence tools for the agent | `opsbridge_mcp` |
| Casework MCP | `opsbridge mcp-casework` | Approved follow-up task creation, read-back, reconciliation | `opsbridge_mcp` |
| Purchasing simulator | `opsbridge simulator` | Stand-in external purchasing and task systems (synthetic) | own SQLite |
| Bootstrap (one-shot) | `alembic upgrade head`, `seed-demo`, `init-keys` | Schema, demo tenants, delegation key | `opsbridge_migrator` |

Code layout: `domain/` (money, units, policy, matching, decision — pure), `evidence/`
(parsers, ingestion, citations, retrieval), `workflow/` (jobs, investigation, execution,
worker, retention), `agents/` (model contract, adapters, routing, validators), `mcp_services/`,
`connectors/`, `services/` (use cases behind the API), `api/`, `persistence/` (models,
migrations, RLS), `telemetry/`, `evaluation/`, `operations/`.

## Data flow: investigation

```
browser ──POST /cases/{id}/investigations──▶ API ──(same transaction)──▶ jobs row (+ trace context)
                                                                              │ leased (SKIP LOCKED, fencing)
worker ◀──────────────────────────────────────────────────────────────────────┘
  collect_context   simulator HTTP (HMAC) → bytes → bounded parser → pack mapping → records, chunks
  retrieve_policy   effective-dated policy version for the invoice date
  reconcile_records deterministic matcher → findings with evidence references → rule route
  explain_findings  optional model (mock/Anthropic/Ollama) + Records MCP search; validators; fallback
  propose_follow_up optional model draft; must agree with the rule route
  validate_proposal → investigation version (findings, citations, manifest digest), case status
```

Each step is durable (`workflow_steps`): a restarted worker resumes after the last completed
step; a worker that lost its lease cannot write results (fencing token).

## Data flow: approval and execution

```
investigator ─▶ proposal v1 (canonical payload digest) ─▶ approver (different user if policy requires)
approve: approval bound to version, digest, policy, source manifest, expiry
         + operation + outbox event + dispatch job — one transaction
worker dispatch: re-check preconditions → attempt row committed → Casework MCP (service token +
  delegation JWT) → connector create → read-back → confirmed | failed | outcome_unknown
ambiguous result: reconcile by declared capability (idempotency key / strong / eventual lookup)
```

See docs/execution-and-recovery.md for every rule and its test.

## Trust boundaries

Browser ⇄ API (HttpOnly session cookie, CSRF token + Origin); API/worker/MCP ⇄ PostgreSQL
(non-bypass roles, FORCE RLS, transaction-local tenant context); worker ⇄ MCP (Keycloak
client-credentials token + Ed25519 delegation); services ⇄ simulator/GitHub (HMAC or token,
allow-listed repository); hosted model (opt-in, redaction, spend ceiling). Documents, tool
output and model output are untrusted data throughout. Threats and controls:
docs/security-and-threat-model.md.

## State machines

**Case** (`services/cases.py::CASE_TRANSITIONS`): `new → collecting_evidence → investigating →
{needs_information | awaiting_review | resolved}`; `awaiting_review → follow_up_pending` when an
approved follow-up is confirmed (the business exception stays open); `resolved` and
`closed_without_resolution` are terminal. Resolution requires evidence, not a completed task.

**Workflow run**: `queued → running → completed | failed`. **Job**: `queued → leased →
succeeded`, or back to `queued` with backoff on a retryable failure, `failed` (definite) or
`dead` (retries exhausted); an expired lease makes the job claimable again.

**Proposal**: `awaiting_approval → approved | rejected | expired | superseded`.
**Operation**: `pending_dispatch → executing → confirmed | failed | outcome_unknown`, or
`cancelled_before_dispatch`. `outcome_unknown` requires human review; it is never retried
blindly.

## Storage

PostgreSQL holds tenants, memberships, sessions, cases, documents and versions (digests),
chunks, extracted fields, records, investigations, proposals, approvals, operations, attempts,
outbox, jobs, audit, model and tool call records. Original bytes and model/tool payloads are in
a content-addressed artifact store (`<tenant>/<aa>/<sha256>`; local filesystem by default, an
S3-compatible bucket when `OPSBRIDGE_ARTIFACT_S3_BUCKET` is set; reads are re-hashed against
the key). Retention: docs/privacy-and-retention.md.

## Telemetry

Prometheus metrics with bounded labels (API `/metrics`, worker `:9101/metrics`), tenant-scoped
backlogs only through the aggregate `ops.metrics_snapshot()` function; OpenTelemetry traces
from the API request through the job, steps, model/tool calls and connector attempts
(OTLP or JSON-lines export when configured); logs carry trace and correlation ids and pass
through the redacting filter. Alerts and runbooks: docs/operations.md.
