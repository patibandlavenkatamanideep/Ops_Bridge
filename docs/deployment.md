# Deployment

Status (2026-10-15): **local only.** Nothing is deployed to any cloud account. The staging
plan below is a design, not provisioned infrastructure; no template in this repository has
been applied anywhere. Production readiness is not claimed.

| Target | How | Verified |
|---|---|---|
| Local, host processes | `make setup`, `make infra-up`, `make migrate`, `make seed-demo`, `make dev` (UI :5173, API :8000) | yes — `make verify` (15 gates) on macOS + Podman 5.7.1 |
| Local, all containers | `make demo-containers` (stop `make dev` first; UI+API on :8000; own database `opsbridge_demo`) | yes — clean launch from empty volumes, 12 browser tests, recovery drills (docs/operations.md) |
| Local observability | `make observability-up` (Prometheus :9090, Alertmanager :9093, alert sink :9099, Grafana :3000) | yes — alert drills with injected failures, delivered through Alertmanager to the local sink; all 23 dashboard queries ran in Grafana without error |
| Hosted CI | `.github/workflows/ci.yml` (pull requests to main, every push to main, manual dispatch): lint, types, unit, canary, receipts, build, API-type drift; integration; browser; runtime image (build, smoke test, SBOM, fail on fixable critical vulnerabilities) | yes for the first three jobs since PR #1 (run 39000811947 at 1f29197, 2026-10-17); the image job was added 2026-10-22 (see the PR's checks) |
| Staging (AWS, below) | not provisioned | **no** |

## Local containers

One image (`infra/containers/opsbridge.Dockerfile`, built from `backend/uv.lock` and
`frontend/package-lock.json`; Python 3.12.15-slim, Node 24.22 for the UI build, uv 0.10.2)
runs every role as uid 10001. The build context is an allow-list (`.dockerignore`): `.env`,
keys, `var/` and virtual environments never enter the image. Compose profile `app`:

- `bootstrap` (one-shot): `alembic upgrade head`, `seed-demo`, `init-keys`; every other
  service waits for it, so the delegation key is created once.
- `api` (publishes 127.0.0.1:8000, serves the built UI), `worker`, `records-mcp`,
  `casework-mcp`, `simulator` — health checks via `opsbridge probe` (`/readyz`, worker
  `:9101/readyz`, TCP for MCP).
- Volumes: `opsbridge_appdata` (artifacts and keys, shared by API, worker and MCP),
  `opsbridge_simdata`, `opsbridge_pgdata`. `make down` stops containers and keeps volumes.
- OIDC: browsers use `http://localhost:8081` (Keycloak `KC_HOSTNAME`); containers fetch
  metadata, tokens and keys over `http://keycloak:8080` (`OPSBRIDGE_OIDC_DISCOVERY_URL`,
  dynamic backchannel) while validating the public issuer.
- MCP DNS-rebinding protection stays on with the service's own address allowed.

## Staging plan (AWS, not provisioned)

No cloud account was named. This is a small, conventional container option; Azure
Container Apps or GCP Cloud Run with managed PostgreSQL are equivalent alternatives.

**Topology**

```
Internet ─▶ ALB (HTTPS, ACM certificate, optional AWS WAF) ─▶ ECS Fargate service: api (2 tasks)
                                                             │
private subnets: ECS services worker (1–2), records-mcp, casework-mcp (Service Connect, no public route)
                 RDS PostgreSQL 16 (pgvector available; encrypted with KMS; automated backups)
                 S3 bucket for artifacts (SSE-KMS, versioning, Block Public Access) — S3ArtifactStore
identity: customer IdP (OIDC) or Keycloak on ECS with its own RDS database, HTTPS issuer
telemetry: ADOT collector sidecar → CloudWatch / X-Ray (OTLP); Amazon Managed Prometheus
           scraping api /metrics and worker :9101 inside the VPC; CloudWatch Logs
dashboards: infra/observability/grafana/dashboards imported into Amazon Managed Grafana (or
           any Grafana) with the Prometheus data source uid `opsbridge-prometheus`
alerts:    the same rules and routing tree in Amazon Managed Prometheus's alert manager, whose
           receivers are SNS topics (the only type it supports): one for page, subscribed by
           the on-call pager, one for ticket, subscribed by the team's queue or email
egress:   NAT gateway only if a hosted model or the GitHub sandbox is enabled; otherwise none
```

**Starting sizes (not load-tested; adjust from metrics)**

| Component | Size | Notes |
|---|---|---|
| api | 0.5 vCPU / 1 GB × 2 tasks | stateless; sessions in PostgreSQL |
| worker | 0.5 vCPU / 1 GB × 1–2 tasks | concurrency via more tasks (SKIP LOCKED); stop timeout ≥ 30 s (lease) |
| records-mcp, casework-mcp | 0.25 vCPU / 0.5 GB each | internal only |
| RDS | db.t4g.small class, 20 GB gp3, Multi-AZ optional | role passwords per service |
| S3 | Standard storage class, one bucket | artifacts for api and worker through the task role |

Local resident-memory readings were not usable for sizing (macOS memory compression), so
the sizes above are conventional starting points, not measurements.

**Secrets** (AWS Secrets Manager, injected as task secrets; never in images or task
definitions): PostgreSQL passwords for migrator, app, worker and mcp roles (plus the master
user, kept out of the application); OIDC client secret; worker client secret; session secret
(≥ 32 chars); delegation Ed25519 private key (`OPSBRIDGE_DELEGATION_PRIVATE_KEY`, PEM; used directly and
never written to disk — production refuses to start the signer or verifier without it or an
existing key file); metrics token; optional GitHub sandbox token and Anthropic API key (only with the owner's opt-in and
spend ceiling).

**Release qualification (in the repository) and what stays with the owner.** CI qualifies
every PR head and every commit on `main` with the same jobs, including the runtime image
built from the locks with base images pinned by digest, its SBOM (SPDX, a workflow artifact)
and a vulnerability scan that fails on fixable critical findings. Nothing is published:
pushing an image to a registry, signing it and recording provenance belong to a release
workflow that the owner authorizes, and the image promoted to staging must be the one whose
digest CI qualified. Recommended repository settings (the owner's to apply): protect `main`,
require the four CI jobs and one review before merging, disallow force-pushes. Dependabot
(`.github/dependabot.yml`) proposes weekly updates as pull requests; each must pass CI.

**Deployed settings checks.** `OPSBRIDGE_ENV=staging` (synthetic data; the purchasing
simulator is allowed) and `OPSBRIDGE_ENV=production` refuse to start unless: HTTPS public URL
and issuer, no localhost issuer, simulator fault controls off, session secret of at least 32
characters, an S3 artifact bucket with server-side encryption requested (`AES256` or
`aws:kms`), quotas on, the rate limit shared across instances, and `trusted_proxies` naming the
load balancer. `production` also refuses demo user credentials and, because no production
purchasing-source adapter exists yet, refuses to start at all: the staging plan uses
`staging`. These are settings checks: they do not show that the bucket actually encrypts or
that the CIDRs are really the load balancer's. Cookies are `Secure` in both. A deployment
keeps the default per-user limit (`OPSBRIDGE_RATE_LIMIT_PER_MINUTE`, 240) unless it has
reason to change it; the development `.env` raises it to 1200 because the browser tests sign
in as the same demo users many times a minute.

**Cost considerations** (no prices quoted here; use the AWS pricing calculator for the chosen
region): Fargate vCPU-hours and GB-hours per task; RDS instance hours, storage and backup
retention; ALB hours and LCUs; NAT gateway hours and data processing (avoid if no egress is
needed); S3 storage and requests, KMS requests for SSE-KMS; CloudWatch log ingestion and retention; Secrets Manager per secret;
hosted model usage (bounded by the configured spend ceiling).

**Artifact storage (S3; implemented, tested locally against SeaweedFS, not against AWS).**
Set `OPSBRIDGE_ARTIFACT_S3_BUCKET` on api and worker, with `OPSBRIDGE_ARTIFACT_S3_PREFIX` and
`OPSBRIDGE_ARTIFACT_S3_REGION` (`OPSBRIDGE_ARTIFACT_S3_ENDPOINT_URL` only for non-AWS
S3-compatible stores). Keys are the same tenant-prefixed, content-addressed keys as the local
store; every read is re-hashed against its key; credentials come from the task role (the
standard AWS chain), never from settings.

- Encryption: bucket default encryption with a customer-managed KMS key; optionally
  `OPSBRIDGE_ARTIFACT_S3_SSE=aws:kms` and `OPSBRIDGE_ARTIFACT_S3_KMS_KEY_ID` so every upload
  also requests it. OpsBridge does not encrypt bytes itself. The SSE parameters are only
  unit-tested as passed through; SeaweedFS rejects them without its own KMS setup.
- Task role: `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `<bucket>/<prefix>*`;
  `s3:ListBucket` on the bucket limited to the prefix (without it S3 answers 403 instead of
  404 for a missing object, and uploads fail); `kms:GenerateDataKey` and `kms:Decrypt` on the
  key. Block Public Access on; a bucket policy denying requests without TLS.
- Versioning and retention: with versioning on (useful for restore), a retention delete only
  adds a delete marker. Add a lifecycle rule that expires noncurrent versions after the backup
  window and state that window in the retention policy, or deleted documents stay recoverable.
- Isolation: one bucket and one task role for all tenants, as with the local directory; tenant
  separation comes from the store's key checks and the RLS-protected artifact manifest, not
  per-tenant IAM.
- The free-space gauge and `OpsBridgeArtifactDiskLow` apply to the local store only; watch the
  bucket with S3 storage metrics.

**Database roles.** Run the SQL in `infra/compose/postgres/10-opsbridge-roles.sh` and
`20-opsbridge-databases.sh` once with the master user (roles are NOSUPERUSER NOBYPASSRLS;
`opsbridge_ops_reader` is NOLOGIN), then migrations as `opsbridge_migrator`.

**Rollout and rollback.** Build the image in CI from the locks, tag it with the commit SHA,
run the bootstrap task (migrations) first, then update the services. ECS deployment
circuit breaker with automatic rollback to the previous task definition. Migrations stay
backward compatible for one release. Data rollback = restore the RDS snapshot and the bucket's objects (versions or a replicated
copy) to the same point into a new environment, verify with
`opsbridge verify-restore --database D --artifact-bucket B`, then switch (docs/operations.md).

**Before any staging deployment:** owner authorization and account; decide IdP; check the S3
store's encryption, IAM and lifecycle settings against the real bucket (tested locally against
SeaweedFS only); connect the page and ticket receivers to real on-call and queue services and
repeat the routing drill there (locally they reach only the alert sink);
add the WAF/rate-limit edge (application rate limits are shared across API tasks through
PostgreSQL but are not a DDoS defence); set `OPSBRIDGE_TRUSTED_PROXIES` to the ALB's subnet
CIDRs, or every client is counted under the ALB's address; run the restore drill
there with synthetic data.
