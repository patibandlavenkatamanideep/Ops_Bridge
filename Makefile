# OpsBridge developer commands. Direct equivalents are listed in docs/developer-commands.md.
SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT := $(abspath .)
COMPOSE_FILE := infra/compose/docker-compose.yml
# Regenerable environments live in *.nosync dirs so a cloud-synced checkout never evicts them.
export UV_PROJECT_ENVIRONMENT := .venv.nosync
export PYTHONPYCACHEPREFIX := $(ROOT)/.pycache.nosync
UV := uv --directory backend
PY := $(UV) run --frozen
CONTAINER_ENV := eval "$$(scripts/container-env.sh)"
COMPOSE := $(CONTAINER_ENV) && docker compose --env-file .env -f $(COMPOSE_FILE)

.PHONY: help doctor setup dev infra-up migrate seed-demo demo lint typecheck test test-integration \
        test-e2e eval-offline verify build down reset-demo openapi alerts-test observability-up backup restore-drill image demo-containers eval-canary

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

doctor: ## Check prerequisites without printing secrets
	@python3 scripts/doctor.py

setup: ## Install locked dependencies; create .env without overwriting secrets
	python3 scripts/setup_env.py
	$(UV) sync --frozen
	cd frontend && npm ci --no-audit --no-fund
	mkdir -p var/artifacts var/keys

infra-up: ## Start PostgreSQL, Keycloak and the local S3-compatible store; wait until healthy
	$(COMPOSE) up -d --build --wait postgres keycloak seaweedfs
	$(COMPOSE) exec -T postgres bash /docker-entrypoint-initdb.d/20-opsbridge-databases.sh >/dev/null

migrate: ## Apply database migrations (dev, test and eval databases)
	$(PY) alembic upgrade head
	$(PY) alembic -x db=opsbridge_test upgrade head
	$(PY) alembic -x db=opsbridge_eval upgrade head

seed-demo: ## Idempotently load ONLY the designated synthetic demo tenants
	$(PY) python -m opsbridge.cli seed-demo

dev: infra-up migrate ## Start infrastructure, then API/worker/simulator/MCP/UI with health checks
	$(PY) python $(ROOT)/scripts/devstack.py

demo: infra-up migrate seed-demo ## Start the stack, migrate and seed non-destructively, then run it
	$(PY) python $(ROOT)/scripts/devstack.py --seed-records

lint: ## Ruff + ESLint
	$(PY) ruff check src tests
	$(PY) ruff format --check src tests
	cd frontend && npm run lint

typecheck: ## mypy --strict + tsc
	$(PY) mypy src
	cd frontend && npm run typecheck

test: ## Unit tests (no services, no network)
	$(PY) pytest -q -m "not integration and not keycloak"
	cd frontend && npm test

test-integration: ## Integration tests against real PostgreSQL, Keycloak, HTTP and MCP (fails if services are down)
	OPSBRIDGE_REQUIRE_INTEGRATION=1 $(PY) pytest -q -m "integration or keycloak"

test-e2e: ## Playwright browser tests against the running dev stack
	cd frontend && npm run e2e

eval-offline: ## Replay the declared benchmark offline (no paid calls, network egress blocked)
	$(PY) python -m opsbridge.cli eval-bench --split heldout --replay
	$(PY) python -m opsbridge.cli eval-bench --split security --replay
	$(PY) python -m opsbridge.cli eval-verify

eval-canary: ## Offline regression check (rules path, dev/validation/security) against the pinned baseline
	$(PY) python -m opsbridge.cli eval-canary

openapi: ## Regenerate OpenAPI document and frontend types
	$(PY) python -m opsbridge.cli export-openapi --out ../frontend/src/api/openapi.json
	cd frontend && npm run gen:api

verify: ## Run every available local gate and name missing prerequisites
	@python3 scripts/verify.py

build: ## Build backend wheel and frontend bundle from the locked dependencies
	$(UV) build --wheel --out-dir ../dist
	cd frontend && npm run build

PROMETHEUS_IMAGE := docker.io/prom/prometheus:v3.13.4

alerts-test: ## Check and unit-test the Prometheus alert rules (promtool in a container)
	$(CONTAINER_ENV) && docker run --rm -v "$(ROOT)/infra/observability/prometheus":/etc/prom:ro --workdir /etc/prom --entrypoint promtool $(PROMETHEUS_IMAGE) check rules alerts.yml
	$(CONTAINER_ENV) && docker run --rm -v "$(ROOT)/infra/observability/prometheus":/etc/prom:ro --workdir /etc/prom --entrypoint promtool $(PROMETHEUS_IMAGE) test rules alerts.test.yml

observability-up: ## Start Prometheus (scrapes make dev processes; alerts at http://localhost:9090/alerts)
	$(COMPOSE) --profile observability up -d prometheus

image: ## Build the application image (API+UI, worker, MCP, simulator) from the locks
	$(CONTAINER_ENV) && docker build -f infra/containers/opsbridge.Dockerfile -t opsbridge/app:local .

demo-containers: image ## Run the whole stack in containers (stop `make dev` first); UI on http://localhost:8000
	$(COMPOSE) --profile app up -d --wait postgres keycloak
	$(COMPOSE) exec -T postgres bash /docker-entrypoint-initdb.d/20-opsbridge-databases.sh >/dev/null
	$(COMPOSE) --profile app up -d --wait

backup: ## Back up the local database and artifact store to var/backups/<UTC timestamp>
	python3 scripts/backup_restore.py backup

restore-drill: ## Restore the latest backup into an isolated database, verify, and drop it
	python3 scripts/backup_restore.py restore-drill

down: ## Stop containers WITHOUT deleting persistent volumes
	$(COMPOSE) stop

reset-demo: ## DESTRUCTIVE: delete the local demo database volume (requires CONFIRM=delete-local-demo-data)
	@if [ "$(CONFIRM)" != "delete-local-demo-data" ]; then \
	  echo "Refusing: run 'make reset-demo CONFIRM=delete-local-demo-data' to delete local demo data."; exit 2; fi
	@grep -q '^OPSBRIDGE_ENV=development' .env || { echo "Refusing: OPSBRIDGE_ENV is not development"; exit 2; }
	$(COMPOSE) down --volumes
