#!/usr/bin/env bash
# Idempotent: creates any missing OpsBridge database and (re)applies its grants.
# Runs on first initialization (after 10-opsbridge-roles.sh) and from `make infra-up`
# (docker compose exec), so an existing volume gains databases added later.
#   opsbridge       development/demo data
#   opsbridge_test  integration tests (truncated per test)
#   opsbridge_eval  offline benchmark runs (truncated per run)
#   opsbridge_demo  the all-container demo (make demo-containers), apart from `make dev` data,
#                   because the containers keep their artifacts in their own volume
set -euo pipefail

# NOLOGIN role that owns the aggregate-only metrics function (migration 0004). It has no
# login, no BYPASSRLS and only column-level SELECT granted by the migration.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsbridge_ops_reader') THEN
    CREATE ROLE opsbridge_ops_reader NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
GRANT opsbridge_ops_reader TO opsbridge_migrator;
SQL

for db in opsbridge opsbridge_test opsbridge_eval opsbridge_demo; do
  exists=$(psql -tA --username "$POSTGRES_USER" --dbname postgres \
    -c "SELECT 1 FROM pg_database WHERE datname = '${db}'")
  if [ "$exists" != "1" ]; then
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
      -c "CREATE DATABASE ${db} OWNER opsbridge_migrator"
  fi
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'SQL'
-- Extensions need superuser; create them here so migrations run as the migrator.
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO opsbridge_app, opsbridge_worker, opsbridge_mcp;
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" -c \
    "REVOKE CONNECT ON DATABASE ${db} FROM PUBLIC; GRANT CONNECT ON DATABASE ${db} TO opsbridge_migrator, opsbridge_app, opsbridge_worker, opsbridge_mcp;"
done
