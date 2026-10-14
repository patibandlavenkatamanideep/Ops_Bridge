#!/usr/bin/env bash
# Runs once, on first initialization of the data volume, as the bootstrap superuser.
# Creates non-superuser roles; application roles never get BYPASSRLS or ownership.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v migrator_pw="$OPSBRIDGE_DB_MIGRATOR_PASSWORD" \
  -v app_pw="$OPSBRIDGE_DB_APP_PASSWORD" \
  -v worker_pw="$OPSBRIDGE_DB_WORKER_PASSWORD" \
  -v mcp_pw="$OPSBRIDGE_DB_MCP_PASSWORD" \
  -v keycloak_pw="$KEYCLOAK_DB_PASSWORD" <<'SQL'
CREATE ROLE opsbridge_migrator LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD :'migrator_pw';
CREATE ROLE opsbridge_app      LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD :'app_pw';
CREATE ROLE opsbridge_worker   LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD :'worker_pw';
CREATE ROLE opsbridge_mcp      LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD :'mcp_pw';
CREATE ROLE keycloak           LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB PASSWORD :'keycloak_pw';

CREATE DATABASE opsbridge      OWNER opsbridge_migrator;
CREATE DATABASE opsbridge_test OWNER opsbridge_migrator;
CREATE DATABASE keycloak       OWNER keycloak;
SQL

for db in opsbridge opsbridge_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'SQL'
-- Extensions need superuser; create them here so migrations run as the migrator.
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO opsbridge_app, opsbridge_worker, opsbridge_mcp;
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" -c \
    "REVOKE CONNECT ON DATABASE ${db} FROM PUBLIC; GRANT CONNECT ON DATABASE ${db} TO opsbridge_migrator, opsbridge_app, opsbridge_worker, opsbridge_mcp;"
done
