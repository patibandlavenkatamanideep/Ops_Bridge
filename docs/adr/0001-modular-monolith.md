# ADR 0001: Modular monolith with a separate worker and narrow MCP services

Status: accepted (2026-10-14)

## Context
One engineer must be able to explain, run, and operate the system. The workflow has
long-running investigation steps and external side effects that must survive restarts.

## Decision
- One Python package `opsbridge` (backend/src/opsbridge) containing domain rules,
  persistence, workflow, evidence, agents, connectors, privacy, audit, evaluation and
  telemetry modules.
- Processes started from that package: `api` (FastAPI), `worker` (leased PostgreSQL jobs),
  `records-mcp` and `casework-mcp` (MCP streamable-HTTP servers), and the purchasing
  `simulator` (independent FastAPI app with its own SQLite store, standing in for an
  external system).
- No Redis, no separate vector database, no workflow engine, no agent framework.
  PostgreSQL holds state, jobs, outbox, audit events, and lexical search; pgvector is used
  only when semantic retrieval is enabled.

## Consequences
- Fewer moving parts; transactional outbox is simple because jobs live in the same database.
- Throughput is bounded by PostgreSQL job polling; acceptable for the expected volume
  (hundreds of cases/day per tenant is an assumption, not a measurement).
- Adding a queue or workflow vendor later requires a measured need and a new ADR.
