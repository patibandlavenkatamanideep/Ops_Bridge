# Developer commands

`make <target>` is the primary interface. Each target maps to direct commands for
environments without `make`. Run from the repository root.

Environment used by every Python command:

```bash
export UV_PROJECT_ENVIRONMENT=.venv.nosync
export PYTHONPYCACHEPREFIX="$PWD/.pycache.nosync"
eval "$(scripts/container-env.sh)"   # selects Docker, or the Podman machine socket
```

| make target | Direct equivalent | Verified on |
|---|---|---|
| `doctor` | `python3 scripts/doctor.py` | macOS 26.2 arm64 |
| `setup` | `python3 scripts/setup_env.py && uv --directory backend sync --frozen && (cd frontend && npm ci)` | macOS 26.2 arm64 |
| `infra-up` | `docker compose --env-file .env -f infra/compose/docker-compose.yml up -d --build --wait postgres keycloak seaweedfs`, then `docker compose … exec -T postgres bash /docker-entrypoint-initdb.d/20-opsbridge-databases.sh` (idempotent; creates missing databases) | macOS + Podman 5.7.1 |
| `migrate` | `uv --directory backend run --frozen alembic upgrade head` (and `-x db=opsbridge_test`, `-x db=opsbridge_eval`) | macOS |
| `seed-demo` | `uv --directory backend run --frozen python -m opsbridge.cli seed-demo` | macOS |
| `dev` | `infra-up`, `migrate`, then `uv --directory backend run --frozen python scripts/devstack.py` (upserts `fixtures/scenarios/dev.yaml` into the simulator before starting it; `--seed-records`, used by `make demo`, loads every corpus) | macOS; Linux in hosted CI |
| `lint` | `ruff check src tests && ruff format --check src tests` (in backend via uv run) and `npm run lint` (frontend) | macOS |
| `typecheck` | `mypy src` (backend) and `npm run typecheck` (frontend) | macOS |
| `test` | `pytest -q -m "not integration and not keycloak"` and `npm test` | macOS |
| `test-integration` | `OPSBRIDGE_REQUIRE_INTEGRATION=1 pytest -q -m "integration or keycloak"` | macOS |
| `test-e2e` | `cd frontend && npx playwright test` (needs the dev stack running) | macOS, Chromium 156 |
| `eval-canary` | `uv --directory backend run --frozen python -m opsbridge.cli eval-canary` (offline rules path vs `evals/baselines/canary-rules.json`) | macOS |
| `alerts-test` | `docker run --rm -v "$PWD/infra/observability/prometheus":/etc/prom:ro --workdir /etc/prom --entrypoint promtool prom/prometheus:v3.13.4 test rules alerts.test.yml` (and `check rules alerts.yml`); then in `prom/alertmanager:v0.34.1`, `amtool check-config alertmanager.local.yml` and `amtool config routes test --verify.receivers=<expected> <labels>` for each line of `infra/observability/alertmanager/routes.test` | macOS + Podman |
| `observability-up` | `docker compose --env-file .env -f infra/compose/docker-compose.yml --profile observability up -d prometheus alertmanager alert-sink` | macOS + Podman |
| `backup` / `restore-drill` | `python3 scripts/backup_restore.py backup` / `… restore-drill [DIR] [--keep]` | macOS + Podman |
| `image` | `docker build -f infra/containers/opsbridge.Dockerfile -t opsbridge/app:local .` | macOS + Podman |
| `demo-containers` | `image`, then `docker compose … --profile app up -d --wait` (stop `make dev` first) | macOS + Podman |
| `eval-offline` | `uv --directory backend run --frozen python -m opsbridge.cli eval-bench --split heldout --replay`, same for `--split security`, then `… eval-verify` | macOS |
| `verify` | `python3 scripts/verify.py` | macOS |
| `build` | `uv --directory backend build --wheel --out-dir ../dist && (cd frontend && npm run build)` | macOS |
| `down` | `docker compose --env-file .env -f infra/compose/docker-compose.yml stop` | macOS |
| `reset-demo` | `docker compose ... down --volumes` — destructive; the make target requires `CONFIRM=delete-local-demo-data` | not exercised |

Benchmark commands (backend, via `uv run --frozen python -m opsbridge.cli …`; details in
docs/evaluation-plan.md):

| Command | Effect |
|---|---|
| `eval-freeze --benchmark ID` | Writes `evals/manifests/ID.json` (digests of splits, rendering, policies, prompts, scoring, modes, repeats). Refuses to overwrite: a changed plan needs a new id. |
| `eval-bench --split S` | Records a run of split S (rules_only and mock) through the stack into `opsbridge_eval`, seals EvalSeal records, writes `evals/results/ID/<run>.json`. Refuses to start if frozen inputs drifted; refuses a second held-out recording. |
| `eval-bench --split S --replay [--from-run DIR]` | Strict offline replay of a recorded run; fails closed (exit 3) on a missing or diverged response or blocked egress. |
| `eval-verify` | Ledger chain, signatures (committed public key) and every bound artifact; missing files fail. |
| `eval-rules --corpus FILE` | Fast DB-free rules-only check (development only). |
| `eval-canary [--update-baseline]` | Regression check against the pinned canary baseline; updating it is a reviewed change. |

Operations commands: `probe URL` (container health probe), `init-keys` (create the delegation
key once), `verify-restore --database D --artifact-dir /abs/path` or `--artifact-bucket B` (restore
checks; refuses the live databases and relative paths; the bucket uses the configured S3
endpoint, region and prefix).

Linux with Docker is expected to work but has not been exercised locally; hosted CI
results are reported separately in docs/STATUS.md. Windows/WSL has not been exercised.

## Workspace location

Do not keep the checkout in an iCloud-synced folder (`~/Desktop`, `~/Documents` with
"Desktop & Documents" enabled). See docs/adr/0009-workspace-and-environment.md.
