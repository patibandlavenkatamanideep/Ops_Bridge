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

CREATE DATABASE keycloak       OWNER keycloak;
SQL
# OpsBridge databases and their grants: 20-opsbridge-databases.sh (idempotent).

