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
| `infra-up` | `docker compose --env-file .env -f infra/compose/docker-compose.yml up -d --build --wait postgres keycloak` | macOS + Podman 5.7.1 |
| `migrate` | `uv --directory backend run --frozen alembic upgrade head` (and `-x db=opsbridge_test`) | macOS |
| `seed-demo` | `uv --directory backend run --frozen python -m opsbridge.cli seed-demo` | macOS |
| `dev` | `infra-up`, `migrate`, then `uv --directory backend run --frozen python scripts/devstack.py` | macOS |
| `lint` | `ruff check src tests && ruff format --check src tests` (in backend via uv run) and `npm run lint` (frontend) | macOS |
| `typecheck` | `mypy src` (backend) and `npm run typecheck` (frontend) | macOS |
| `test` | `pytest -q -m "not integration and not keycloak"` and `npm test` | macOS |
| `test-integration` | `OPSBRIDGE_REQUIRE_INTEGRATION=1 pytest -q -m "integration or keycloak"` | macOS |
| `test-e2e` | `cd frontend && npx playwright test` (needs the dev stack running) | macOS, Chromium 156 |
| `verify` | `python3 scripts/verify.py` | macOS |
| `build` | `uv --directory backend build --wheel --out-dir ../dist && (cd frontend && npm run build)` | macOS |
| `down` | `docker compose --env-file .env -f infra/compose/docker-compose.yml stop` | macOS |
| `reset-demo` | `docker compose ... down --volumes` — destructive; the make target requires `CONFIRM=delete-local-demo-data` | not exercised |

Linux with Docker is expected to work but has not been exercised locally; hosted CI
results are reported separately in docs/STATUS.md. Windows/WSL has not been exercised.

## Workspace location

Do not keep the checkout in an iCloud-synced folder (`~/Desktop`, `~/Documents` with
"Desktop & Documents" enabled). See docs/adr/0009-workspace-and-environment.md.
