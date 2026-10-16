# Operations

Runbooks for the local stack and the planned staging deployment. Alert rules:
`infra/observability/prometheus/alerts.yml` (each links to a section below). Every number
in "Drills" was observed on 2026-10-15 on a MacBook (Podman 5.7.1) with synthetic data; it
is an observation, not a recovery-time objective.

## Signals

| Where | What |
|---|---|
| API `GET /healthz` | process is up |
| API `GET /readyz` | database and identity provider usable (503 otherwise) |
| API `GET /metrics` | HTTP rate/latency by route template, pool use, artifact storage, dependency gauges (optional bearer token: `OPSBRIDGE_METRICS_TOKEN`) |
| Worker `:9101/healthz` | job loop heartbeat fresh (503 when wedged) |
| Worker `:9101/readyz` | loop and database |
| Worker `:9101/metrics` | jobs by result and duration, queue depth/age, expired leases, aggregate backlogs (`opsbridge_state{metric=…}`), model/tool/connector calls, investigation, approval-wait and execution durations |
| Logs | one line per event with `[trace=… cid=…]`; credentials and identifiers scrubbed |
| Traces | `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP/HTTP) or `OPSBRIDGE_TRACE_FILE` (JSON lines). One trace per request, continued into the worker job; spans carry `opsbridge.case_id`, `run_id`, `operation_id`, `attempt_id`, `remote_ref`, `correlation_id` |
| Audit | `audit_events` (who, on whose behalf, what, outcome) per tenant |

Start Prometheus locally with `make observability-up` (scrapes `make dev` processes;
alerts at http://localhost:9090/alerts). Metric labels never contain tenant, case or user
ids; use the correlation id from an error response or log line to find the trace.

## API or worker down

`OpsBridgeTargetDown`. Check the process (`make dev` output, or `docker compose ps` for
`make demo-containers`). The worker exits only on SIGTERM; a database outage makes it back
off, not exit. Containers restart on their own exit (`restart: unless-stopped`); an
operator `kill`/`stop` is not restarted. Jobs held by a dead worker become claimable when
their lease (30 s) expires; durable steps resume and fencing prevents the old worker from
writing late results.

## Dependency unavailable

`OpsBridgeDependencyDown`, `OpsBridgeStateUnknown`. `/readyz` names the failing check.
Identity provider down: sign-in fails with a 503 page; existing sessions keep working (server
side) until they expire. Database down: API requests fail with 503, the worker logs
`database unavailable … retrying in Ns` and resumes on its own.

## Queue stalled

`OpsBridgeQueueStalled`, `OpsBridgeExpiredLeases`. Is a worker running and ready? Then look
at the oldest jobs (superuser or a read-only operator role):

```sql
SELECT kind, status, attempts, max_attempts, run_after, lease_expires_at, left(last_error, 200)
FROM jobs WHERE status IN ('queued', 'leased') ORDER BY run_after LIMIT 20;
```

A kind with no registered handler is never claimed (a new job kind deployed before the
worker). Expired leases are reclaimed automatically; persistent expiry means jobs run longer
than the lease — raise `OPSBRIDGE_WORKER_LEASE_SECONDS` (it must stay ≥ 2 × connector
timeout).

## Retries and dead jobs

`OpsBridgeJobsExhausted`, `OpsBridgeInvestigationFailures`. Retryable failures back off
exponentially with jitter up to `max_attempts` (5). An exhausted investigation marks the run
`failed` (the case shows the error and a re-run button); an exhausted dispatch marks the
operation `outcome_unknown` for review — never a blind re-send. `last_error` holds the
reason; the trace for the job shows which step failed.

## Ambiguous outcomes

`OpsBridgeOutcomeUnknown`. An operation whose external effect cannot be confirmed (timeout
after a possible commit with no reliable lookup, read-back mismatch, exhausted dispatch)
waits for a person. In the case's "Proposals & execution" tab the reviewer sees the attempt
history and records what they found in the external system: the task exists (with its
remote reference → `confirmed`) or it does not (→ `failed`). Do not create the task again by hand before checking the external system for the
operation marker (`opsbridge-op:<operation key>`). Details: docs/execution-and-recovery.md.

## Approval backlog

`OpsBridgeApprovalBacklogAging`. Proposals wait in `awaiting_approval` until the policy's
approval lifetime (furniture 30 min, IT 20 min) passes; expired proposals must be re-proposed. Check that approvers with
the right scope exist (Tenant settings → Members).

## Connector failures

`OpsBridgeConnectorFailures`. Definite failures (4xx) mean the request was rejected —
check the attempt `error_category`; ambiguous failures (timeouts, 5xx) trigger
reconciliation. GitHub rate limits are definite and retryable. For the simulator, check
`/_control/stats/<org>` and that no fault injection is active.

## Model errors

`OpsBridgeModelErrors`. The workflow falls back to template explanations and rule drafts;
findings and next actions are unaffected. Check `model_calls.outcome`/`error`
(`rejected_output` = validator rejection; `routing_blocked` = entitlement/policy; `timeout`).

## API errors

`OpsBridgeApiErrors`, `OpsBridgeApiLatency`. Each 5xx response carries a correlation id;
search logs for `cid=<id>` and the trace with that `opsbridge.correlation_id`.

## Capacity

`OpsBridgeDbPoolSaturated`, `OpsBridgeArtifactDiskLow`, `OpsBridgeProcessMemoryHigh`. Pool
size is `OPSBRIDGE_DB_POOL_SIZE` (+2 overflow) per process. Artifact storage grows with
documents and model/tool payloads; retention (Tenant settings) deletes them by age.

## Migrations

`make migrate` (Alembic, migrator role; dev, test and eval databases). Migrations are
forward-only in practice: each has a `downgrade()`, but downgrades are not exercised against
production-like data. Release order: migrate, then deploy code that needs the new schema;
keep schema changes backward compatible for one release so the previous image can still run.
New cluster-level roles (0004 needs `opsbridge_ops_reader`) come from
`infra/compose/postgres/20-opsbridge-databases.sh` (idempotent; `make infra-up` runs it).

## Backup and restore

- `make backup` → `var/backups/<UTC>/`: `opsbridge.dump` (`pg_dump -Fc` as the container
  superuser, because RLS is forced for every other role), `artifacts.tar.gz`, `manifest.json`
  (digests, row counts, migration revision).
- `make restore-drill` → restores the latest backup into `opsbridge_restore_drill`, compares
  row counts and revision, checks every table still has forced RLS, extracts artifacts to a
  scratch directory, runs `opsbridge verify-restore` (every retained artifact re-hashed,
  every latest-investigation citation resolved, application roles see no rows without a
  tenant context), writes `restore-drill.json`, drops the drill database.
- Restoring for real: stop API and workers, `pg_restore --clean` into `opsbridge` as the
  superuser, restore the artifact tar into the artifact directory, start, run
  `verify-restore` against the restored database first. Content deleted by retention after
  the backup reappears from an older backup; re-run the retention sweep after a restore.

## Rollback

Code: redeploy the previous image tag (images are built from the locks;
`make image`). Schema: prefer forward fixes; `alembic downgrade -1` only for a migration
known to be reversible without data loss. Data: restore from backup (above). A dispatched
external action cannot be rolled back locally; record a compensating follow-up instead.

## Drills (observed 2026-10-15)

| Drill | Injected | Observed |
|---|---|---|
| Alert: identity provider down | `docker compose stop keycloak` | `OpsBridgeDependencyDown{dependency="identity_provider"}` firing after 145 s; API `/readyz` 503; resolved 30 s after restart; `/readyz` 200 |
| Alert: worker down | SIGTERM to the worker (dev stack then stopped the API too) | `OpsBridgeTargetDown` for worker after 65 s (and API); resolved 25 s after restart |
| Alert rules | synthetic series | `promtool test rules`: 9 test cases pass; caught a disk-ratio rule that could never fire (label mismatch) |
| Unplanned | none | `OpsBridgeArtifactDiskLow` went pending on the development laptop (23 GiB free of 228 GiB, 10%); a real condition of the host disk, not of OpsBridge (7 MB) |
| Clean launch | empty volumes, separate compose project, images prebuilt | all services healthy in 38 s; 12 browser tests (phases 1, 2, 4, 6) passed against the containers on :8000 |
| Clean launch, hardened (2026-10-16) | same, with read-only root filesystems, no capabilities, no-new-privileges | healthy in 37 s; writing outside `/app/var` refused; the same 12 browser tests passed |
| Worker process exit | SIGTERM inside the container | restart policy restarted it; healthy after 7 s; (`docker kill` counts as an operator stop and is not restarted) |
| Database restart | `docker restart` PostgreSQL under the running stack | API `/readyz` 200 again after 3 s; worker logged one backoff and continued without restarting |
| Backup + restore | `make backup`, `make restore-drill` | 1,408 rows, 408 KB dump (0.6 s); restore 1.2 s; counts and revision equal; RLS forced on all tables; 124 artifact rows (8 unique files) re-hashed; citations valid; application roles saw 0 rows without tenant context; tampering one artifact failed the check |

These were single runs on a small synthetic dataset; timings will differ with data volume.
