# Deployment

Status (2026-10-15): **local only.** Nothing is deployed to any cloud account. The staging
plan below is a design, not provisioned infrastructure; no template in this repository has
been applied anywhere. Production readiness is not claimed.

| Target | How | Verified |
|---|---|---|
| Local, host processes | `make setup`, `make infra-up`, `make migrate`, `make seed-demo`, `make dev` (UI :5173, API :8000) | yes — `make verify` (12 gates) on macOS + Podman 5.7.1 |
| Local, all containers | `make demo-containers` (stop `make dev` first; UI+API on :8000) | yes — clean launch from empty volumes, 12 browser tests, recovery drills (docs/operations.md) |
| Local observability | `make observability-up` (Prometheus :9090) | yes — alert drills with injected failures |
| Hosted CI | `.github/workflows/ci.yml` (pull requests to main, manual dispatch) | **not run** — validated with actionlint only |
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
                 EFS (encrypted) mounted at /app/var for artifacts — the current LocalArtifactStore
identity: customer IdP (OIDC) or Keycloak on ECS with its own RDS database, HTTPS issuer
telemetry: ADOT collector sidecar → CloudWatch / X-Ray (OTLP); Amazon Managed Prometheus
           scraping api /metrics and worker :9101 inside the VPC; CloudWatch Logs
egress:   NAT gateway only if a hosted model or the GitHub sandbox is enabled; otherwise none
```

**Starting sizes (not load-tested; adjust from metrics)**

| Component | Size | Notes |
|---|---|---|
| api | 0.5 vCPU / 1 GB × 2 tasks | stateless; sessions in PostgreSQL |
| worker | 0.5 vCPU / 1 GB × 1–2 tasks | concurrency via more tasks (SKIP LOCKED); stop timeout ≥ 30 s (lease) |
| records-mcp, casework-mcp | 0.25 vCPU / 0.5 GB each | internal only |
| RDS | db.t4g.small class, 20 GB gp3, Multi-AZ optional | role passwords per service |
| EFS | bursting throughput | artifact volume shared by api/worker/mcp |

Local resident-memory readings were not usable for sizing (macOS memory compression), so
the sizes above are conventional starting points, not measurements.

**Secrets** (AWS Secrets Manager, injected as task secrets; never in images or task
definitions): PostgreSQL passwords for migrator, app, worker and mcp roles (plus the master
user, kept out of the application); OIDC client secret; worker client secret; session secret
(≥ 32 chars); delegation Ed25519 private key (today a file path — needs an entrypoint that
writes the injected secret to a tmpfs file with 0600, a gap to close before staging); metrics
token; optional GitHub sandbox token and Anthropic API key (only with the owner's opt-in and
spend ceiling).

**Production settings checks** (`OPSBRIDGE_ENV=production` refuses to start otherwise): HTTPS
public URL and issuer, no localhost issuer, no demo user credentials, simulator fault
controls off, session secret length. Cookies are `Secure` behind HTTPS.

**Cost considerations** (no prices quoted here; use the AWS pricing calculator for the chosen
region): Fargate vCPU-hours and GB-hours per task; RDS instance hours, storage and backup
retention; ALB hours and LCUs; NAT gateway hours and data processing (avoid if no egress is
needed); EFS storage; CloudWatch log ingestion and retention; Secrets Manager per secret;
hosted model usage (bounded by the configured spend ceiling).

**Database roles.** Run the SQL in `infra/compose/postgres/10-opsbridge-roles.sh` and
`20-opsbridge-databases.sh` once with the master user (roles are NOSUPERUSER NOBYPASSRLS;
`opsbridge_ops_reader` is NOLOGIN), then migrations as `opsbridge_migrator`.

**Rollout and rollback.** Build the image in CI from the locks, tag it with the commit SHA,
run the bootstrap task (migrations) first, then update the services. ECS deployment
circuit breaker with automatic rollback to the previous task definition. Migrations stay
backward compatible for one release. Data rollback = restore RDS snapshot + EFS backup into a
new environment, verify with `opsbridge verify-restore`, then switch (docs/operations.md).

**Before any staging deployment:** owner authorization and account; decide IdP; close the
delegation-key file gap; consider an S3 artifact store (not implemented) instead of EFS;
add the WAF/rate-limit edge (application rate limits are per process); run the restore drill
there with synthetic data.
