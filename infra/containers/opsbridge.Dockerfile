# OpsBridge application image. One image runs the API (serving the built UI), worker, MCP
# services, simulator and the one-shot bootstrap; the command selects the role.
# Build from the repository root: docker build -f infra/containers/opsbridge.Dockerfile .
# The repository layout is kept under /app so paths resolve as in development.
# Base images are pinned by index digest (tag kept for reading); Dependabot proposes updates.

FROM docker.io/library/node:26.12.1-bookworm-slim@sha256:88e480776c6149ff1d098cd304f44bf18057fb5d284a3329b78162d29821e28c AS ui
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS python
COPY --from=ghcr.io/astral-sh/uv:0.10.2@sha256:94a23af2d50e97b87b522d3cea24aaf8a1faedec1344c952767434f69585cbf9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/backend/.venv
WORKDIR /app/backend
# Dependencies first (cached layer), from the committed lock only.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/alembic.ini ./
COPY backend/src ./src
RUN uv sync --frozen --no-dev

FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
# Optional local OCR for PDFs without a text layer (off unless OPSBRIDGE_OCR_ENABLED=true):
# Debian's tesseract with English data, pinned. OCR text is never authoritative.
# The base image carries the fixes for CVE-2026-84782 (OpenSSL 3.0.22-1~deb12u2) and
# CVE-2026-109518 (apt 2.6.2), both HIGH; the CI gate stops fixable HIGH findings. To raise a
# package past the base image's, pin it here, and drop the pin once the base carries it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr=5.3.0-2 tesseract-ocr-eng=1:4.1.0-2 \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 opsbridge \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin opsbridge
COPY --from=python /app/backend /app/backend
COPY packs /app/packs
COPY fixtures /app/fixtures
COPY evals /app/evals
COPY --from=ui /app/frontend/dist /app/frontend/dist
# Only the volumes mounted under /app/var are writable by the service user. Each mount point
# is made here, since a new volume takes the owner of the directory it is first mounted on.
RUN mkdir -p /app/var/artifacts /app/var/keys /app/var/delegation-public && chown -R 10001:10001 /app/var && chmod 0700 /app/var/keys
ENV PATH=/app/backend/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OPSBRIDGE_WORKER_HTTP_HOST=0.0.0.0
USER 10001:10001
WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "-m", "opsbridge.cli", "api", "--host", "0.0.0.0", "--port", "8000", "--serve-frontend", "--static", "/app/frontend/dist"]
