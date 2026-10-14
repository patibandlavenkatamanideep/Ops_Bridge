# OpsBridge status

Last updated: 2026-10-14 · Branch: `feat/opsbridge-foundation`

## Summary by validation level

| Level | State |
|---|---|
| Local application complete | not yet |
| Real model validated | not evaluated (no live opt-in, no credentials) |
| External connector (GitHub sandbox) validated | not evaluated (no repo/token/authorization) |
| Staging deployed | no |
| Operator pilot measured | no |

## Workspace inventory (Phase 0, 2026-10-14)

- Initial contents: `CLAUDE.md`, `OPSBRIDGE_BUILD_PROMPT.md`. No git repository, code,
  dependency files, or tests existed. Initialized git on branch `feat/opsbridge-foundation`.
- Toolchain found: uv 0.10.2 (manages CPython 3.12.12), Node 25.9.0 / npm 11.12.1,
  Docker CLI 29.8.1 + Compose 5.5.1 (Docker Desktop daemon **not running**),
  Podman 5.7.1 machine **running** with a Docker-compatible socket, GNU Make 3.81,
  git 2.50.1, gh 2.92.0. Homebrew PostgreSQL 16 on :5432 hosts unrelated databases and is
  not used by OpsBridge.
- Missing: Java (Keycloak runs in a container instead), Ollama, Tesseract OCR, pdftotext.
- Network: PyPI, npm, GitHub API, quay.io and Docker Hub reachable. Pulled
  `pgvector/pgvector:pg16` and `quay.io/keycloak/keycloak:26.8.1` through Podman.

## Completed

### Phase 0 — audit, contract, discovery assumptions
- docs/discovery.md, docs/IMPLEMENTATION_PLAN.md, docs/requirements-traceability.md,
  ADRs 0001–0008 under docs/adr/.
- Upstream interfaces inspected locally (scratch venv): evalseal 2.2.0 API/CLI and docs
  at tag v2.2.0; mcp 2.3.0 (`MCPServer`, `Client`, `streamable_http_client`,
  `TokenVerifier`, `AccessToken`).

## Blockers (affect only dependent work)

| Blocker | Blocks | Unblocked by |
|---|---|---|
| No Anthropic credentials / live opt-in | Live hosted-model checks | Owner provides key, purpose and spend ceiling |
| Ollama not installed | Live local-inference checks | Install Ollama and pull a pinned model |
| No GitHub sandbox repo/token/authorization | Live connector run (OB-22 verified_sandbox) | Owner names repo, provides fine-grained token, authorizes writes |
| Tesseract not installed | OCR path for scanned PDFs | Install tesseract; otherwise scanned documents route to needs_review |
| No cloud account/authorization | Staging deployment | Owner authorizes and provides account |
| No operator participants | Pilot measurements | Pilot owner recruits participants |

## Known defects

None recorded yet.

## Next executable task

Phase 1: backend project (uv, FastAPI), pure domain rules (money/units/tolerance/matching)
with tests, PostgreSQL container + roles + migrations with RLS, Keycloak realm, sessions,
case intake/queue API and web shell.
