#!/usr/bin/env bash
# Idempotent: creates any missing OpsBridge database and (re)applies its grants.
# Runs on first initialization (after 10-opsbridge-roles.sh) and from `make infra-up`
# (docker compose exec), so an existing volume gains databases added later.
#   opsbridge       development/demo data
#   opsbridge_test  integration tests (truncated per test)
#   opsbridge_eval  offline benchmark runs (truncated per run)
set -euo pipefail

for db in opsbridge opsbridge_test opsbridge_eval; do
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
