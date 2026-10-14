# Requirements traceability

Statuses: not_started, in_progress, implemented_unverified, verified_local, verified_ci,
verified_sandbox, deployed_staging, pilot_validated, blocked.
Requirement IDs are stable; paths and evidence are updated as work lands.

| ID | Requirement | Acceptance evidence (from brief) | Implementation | Evidence / command | Status | Unresolved limits |
|---|---|---|---|---|---|---|
| OB-01 | Scoped procurement problem and discovery assumptions | Workflow map, user roles, success criteria, assumptions distinguished from interviews | docs/discovery.md | — | implemented_unverified | No interviews conducted; all assumptions unverified |
| OB-02 | Three discrepancy types | Independent correct/incorrect/missing-data fixtures and UI flows | — | — | not_started | |
| OB-03 | Cross-organization portability | Furniture and IT packs run without organization-specific core branches | — | — | not_started | |
| OB-04 | Documents plus structured data | PDF/text/CSV/API evidence with stable provenance | — | — | not_started | |
| OB-05 | Arithmetic and matching rules | Decimal, tolerance, cumulative-receipt, unit and currency tests | — | — | not_started | |
| OB-06 | Evidence retrieval and citations | Authorized retrieval; citations resolve to the exact source version | — | — | not_started | |
| OB-07 | Agent and specialist steps | Bounded workflow with explicit validation and escalation | — | — | not_started | |
| OB-08 | MCP tools | Genuine MCP client/server requests; discovery and denied-call tests | — | — | not_started | |
| OB-09 | Enterprise identity integration | Local OIDC login, sessions, expiry, roles, tiers, and machine identity tests | — | — | not_started | |
| OB-10 | Tenant/resource isolation | DB, worker, retrieval, cache, artifact, and tool negative tests | — | — | not_started | |
| OB-11 | Approval authority | Payload-bound approval, separation of duties, expiry and stale-input tests | — | — | not_started | |
| OB-12 | Durable action execution | Outbox, leases, retries, crash recovery, and ambiguous-outcome tests | — | — | not_started | |
| OB-13 | Attempt-level frozen responses | Every attempt correlated; replay never silently executes live | — | — | not_started | |
| OB-14 | Privacy and local inference | Redaction tests, retention behavior, unavailable-local-model refusal | — | — | not_started | |
| OB-15 | Stateful conversations | Case-scoped history, durable checkpoints, controlled correction and deletion | — | — | not_started | |
| OB-16 | API and perimeter security | CSRF/CORS/rate-limit tests; documented proxy/WAF deployment boundary | — | — | not_started | |
| OB-17 | Evaluation and uncertainty | Held-out comparison, per-case repeats, intervals, calibration limits | — | — | not_started | |
| OB-18 | Provenance and EvalSeal | Actual receipt, fingerprint/config manifest, artifact tamper detection | docs/adr/0007-evalseal-integration.md | — | not_started | |
| OB-19 | Product observability | Correlated traces, cost/latency, queue/error metrics | — | — | not_started | |
| OB-20 | Runtime monitoring | CPU/memory/disk and worker/DB health, meaningful alerts | — | — | not_started | |
| OB-21 | Usable operator interface | Browser tests, keyboard navigation, empty/error/loading states | — | — | not_started | |
| OB-22 | Sandbox adapter | Real HTTP contracts; live run only if explicitly authorized | — | — | not_started | Live run needs repo, token, authorization |
| OB-23 | Offline reproducibility | Fresh local setup and no-key replay; no hidden external calls | — | — | not_started | |
| OB-24 | Deployment and recovery | Container smoke test, migration, backup/restore, rollback documentation | — | — | not_started | |
| OB-25 | FDE handoff and outcomes | Demo, pilot plan, measured results with honest scope | — | — | not_started | |
| OB-26 | Existing-project boundaries | EvalSeal adapter; no mandatory coupling to all prior repositories | docs/adr/0007-evalseal-integration.md | — | in_progress | |
| OB-27 | Bounded compute and spending | Timeouts, quotas, run limits, explicit live opt-in and cost ceiling | — | — | not_started | |
| OB-28 | Honest status and claims | Every claimed result points to evidence; blocked/live/recorded labels preserved | docs/STATUS.md | — | in_progress | |
