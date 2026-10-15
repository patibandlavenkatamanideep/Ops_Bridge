# ADR 0002: Dependency selection and version pins

Status: accepted (2026-10-14). Versions verified against PyPI, npm and quay.io on 2026-10-14.

## Backend (Python 3.12.12 via uv 0.10.2)
| Package | Version | Why | Licence |
|---|---|---|---|
| fastapi | 0.143.1 | API framework required by brief | MIT |
| pydantic / pydantic-settings | 2.14.1 / 2.16.0 | Typed schemas, config | MIT |
| sqlalchemy | 2.1.4 | ORM/core | MIT |
| alembic | 1.20.0 | Migrations | MIT |
| psycopg[binary] | 3.3.6 | PostgreSQL driver | LGPL-3.0 (dynamic use, unmodified) |
| authlib | 1.9.1 | Maintained OIDC client (PKCE, nonce, ID-token validation) | BSD-3 |
| mcp | 2.3.0 | Official MCP Python SDK (`MCPServer`, `Client`, streamable HTTP) | MIT |
| anthropic | 1.13.0 | Official SDK for the hosted adapter | MIT |
| httpx | 0.28.1 | HTTP client for simulator, GitHub, Ollama | BSD-3 |
| pypdf | 6.20.1 | Text-layer PDF extraction | BSD-3 |
| evalseal | 2.2.0 | Evaluation receipts (owner's verified release, tag v2.2.0) | MIT |
| opentelemetry-api/sdk | 1.45.1 | Tracing | Apache-2.0 |
| prometheus-client | 0.26.0 | Metrics endpoint | Apache-2.0/BSD-2 |
| opentelemetry-exporter-otlp-proto-http | 1.45.1 (matches api/sdk) | Optional OTLP trace export (Phase 8); pulls opentelemetry-proto 1.45.1, protobuf 7.36.2, requests 2.34.2, urllib3 2.8.0 | Apache-2.0 / BSD-3 / Apache-2.0 / MIT |
| pytest / ruff / mypy | 9.1.1 / 0.17.0 / 2.4.0 | Quality gates | MIT |

Exact resolved versions, including transitive ones, are in `backend/uv.lock`.

## Frontend (Node 24 LTS in containers; Node 25.9 on the development machine)
| Package | Version | Note |
|---|---|---|
| react / react-dom | 19.3.1 | |
| vite / @vitejs/plugin-react | 8.3.4 / 6.1.2 | |
| typescript | **5.9.3**, not 7.0.2 | typescript-eslint 8.71 supports `<6.1`; openapi-typescript 7.13 peers on `^5` |
| vitest / jsdom | **4.1.11 / 29.1.1** | vitest 5 and jsdom 30 exclude Node 25 in their `engines` |
| @playwright/test | 1.64.1 | Browser tests |
| openapi-typescript | 7.13.0 | Generate API types from the backend OpenAPI document |
| @tanstack/react-query / react-router-dom | 5.104.2 / 7.18.4 | Server state and routing |
| eslint / @eslint/js | 10.12.0 / 10.0.1 | ESLint 9 is marked unsupported by its maintainers |
| eslint-plugin-jsx-a11y-x | 0.2.0 | Fork of jsx-a11y with ESLint 10 support; upstream 6.10.2 (last release 2024) peers only on ESLint ≤ 9. Young project: runtime checks in browser tests remain necessary |

## Infrastructure images
- `pgvector/pgvector:pg16` (PostgreSQL 16 with pgvector).
- `quay.io/keycloak/keycloak:26.8.1` (local OIDC provider, development profile only).
- `prom/prometheus:v3.13.4` (optional observability profile and `promtool` for rule tests;
  a patched minor chosen over the newest `.0`).
- Application image bases (Phase 8): `python:3.12.15-slim-bookworm` (development machine
  runs 3.12.12; same minor, patch differs), `node:24.22.0-bookworm-slim` (UI build stage only),
  `ghcr.io/astral-sh/uv:0.10.2` (the uv version used locally; 0.10.48 appeared in a tag listing
  but does not exist on ghcr.io).
- `rhysd/actionlint:1.7.12` (workflow lint only).

## CI actions (pinned by commit, verified 2026-10-15)
`actions/checkout` v7.0.1 `3d3c42e5…`, `astral-sh/setup-uv` v10.3.0 `1c37ad07…`,
`actions/setup-node` v7.1.0 `949feb24…`, `actions/upload-artifact` v7.0.2 `cf430e03…`;
input names checked against each `action.yml` at those commits.

## Consequences
- TypeScript 7 and vitest 5 are deliberately not used until the lint/type tooling supports them.
- Version bumps must re-run `make verify` and update this table.
