# ADR 0005: PostgreSQL jobs, transactional outbox, leases, and attempt ledger

Status: accepted (2026-10-14)

## Decision
- `jobs` table with `FOR UPDATE SKIP LOCKED` claiming, a lease expiry, and a monotonically
  increasing `fencing_token` per claim. Completion updates require the current token, so a
  worker whose lease expired cannot overwrite the result of the new lease holder.
- Approval consumption, logical `operations` row (`pending_dispatch`), `outbox_events` row,
  and the dispatch job are written in one transaction.
- Every remote call is preceded by a committed `attempts` row in state `started`. If a
  worker crashes after the remote commit, the next worker sees the open attempt and
  reconciles instead of creating again.
- One stable idempotency key per logical operation; each attempt has its own id and a
  `parent_attempt_id`.
- After an ambiguous failure (timeout, connection reset after send) the executor
  reconciles by idempotency key or opaque marker search when the connector declares that
  capability. If reconciliation is unavailable or inconclusive, the operation becomes
  `outcome_unknown` and requires human review. No blind retry.
- Delivery is at-least-once. We do not claim exactly-once execution.

## Consequences
- No external queue is required. Lease durations and polling intervals are configuration.
