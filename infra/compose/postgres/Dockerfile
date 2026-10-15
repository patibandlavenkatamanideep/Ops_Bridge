# PostgreSQL 16 + pgvector with OpsBridge role/database initialization baked in.
# Scripts are copied rather than bind-mounted: some engines (Podman on macOS/virtiofs)
# cannot execute files from host mounts.
FROM docker.io/pgvector/pgvector:pg16
COPY --chmod=0755 10-opsbridge-roles.sh /docker-entrypoint-initdb.d/10-opsbridge-roles.sh
COPY --chmod=0755 20-opsbridge-databases.sh /docker-entrypoint-initdb.d/20-opsbridge-databases.sh
