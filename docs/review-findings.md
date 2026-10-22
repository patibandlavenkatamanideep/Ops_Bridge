# External review findings (baseline f0dbb1a, 2026-10-22)

An external review of `main` at `f0dbb1a` listed 18 findings (F01–F18) and a delivery order:
Phase A (protect core decisions), B (concurrency and failure), C (deployable artifact),
D (one real integration), E (operator value). This file tracks each finding against the
code on `feat/opsbridge-foundation`. Every fix started with a regression test that failed
on the previous source; "verified locally" means real PostgreSQL, the simulator over HTTP
and, where relevant, real Keycloak and browser tests on this machine.

## Status

| ID | Priority | Finding | Status | Regression evidence |
|---|---|---|---|---|
| F01 | P0 | Approval/cancellation rechecks skipped on a retry | Fixed, verified locally | `test_a_retry_rechecks_eligibility_before_any_new_create[*]` (10: definite or proven-absent failure × expiry, revocation, cancellation, edit, policy); `test_a_create_that_landed_is_confirmed_even_after_the_approval_expires` |
| F02 | P0 | Local effect and operation completion commit separately | Fixed, verified locally | `test_a_local_action_takes_effect_once_even_if_the_worker_dies_while_completing` (before the fix the retry wrote a second draft) |
| F03 | P0 | Destination and outgoing text not in the approved payload | Fixed, verified locally | `test_a_settings_change_after_drafting_cannot_redirect_the_approval`, `test_dispatch_refuses_when_destination_or_outgoing_text_changed_after_approval[connector/redaction]`, `test_a_proposal_from_before_destination_binding_must_be_proposed_again`, attempt request includes body |
| F04 | P0 | Corrections/uploads do not invalidate old decisions | Fixed, verified locally | `tests/integration/test_evidence_freshness.py` (7, incl. a correction committed while dispatch waits for the case lock) |
| F05 | P1 | Investigation jobs lack fencing and renewal | Open (Phase B) | — |
| F06 | P1 | Spend and quotas are check-then-act | Open (Phase B) | — |
| F07 | P1 | Invoice identity tenant-wide, destructive normalization | Fixed for supplier scope, look-alikes and idempotency; verified locally | `tests/integration/test_business_identity.py` (3) |
| F08 | P1 | Retention deletes shared bytes | Open (Phase B) | — |
| F09 | P1 | Unverified cookies select rate-limit buckets | Open (Phase B) | — |
| F10 | P1 | Unit inverse accepts a rounded reciprocal | Open (Phase B) | — |
| F11 | P1 | Accepted corrections never applied | Fixed, verified locally | `test_only_fields_matching_uses_can_be_corrected` |
| F12 | P1 | Unreadable replacement leaves old records current | Fixed, verified locally | `test_an_unreadable_replacement_invoice_is_not_matched_on_the_old_extraction[*]` (3) |
| F13 | P1 | Production configuration may keep development defaults | Open (Phase C) | — |
| F14 | P1 | `main` unprotected; CI does not qualify the runtime image | Open (Phase C; branch protection is the owner's setting) | — |
| F15 | P2 | Validators check numbers and citations, not claim meaning | Open | — |
| F16 | P2 | Local artifact reads do not verify digests | Open (Phase B) | — |
| F17 | P2 | `verify` can pass with required gates skipped | Open (Phase C) | — |
| F18 | P2 | UX needs operator testing | Partly: destination and exact outgoing text shown before approval; Correct offered only for correctable fields; supplier and look-alike confirmation at intake | browser suite and accessibility scan pass |

## What changed, and what it does not cover

- F01: every new write (first dispatch, retry after a definite failure, create after
  reconciliation proved absence) passes cancellation and full re-validation; recovering a
  write that may have landed does not. Not covered: the requester's own permissions are
  not re-checked (the policy names no requester permission).
- F02: local actions commit effect, succeeded attempt, outbox event and confirmation
  together; a partial unique index (0010) allows one applied local action per operation.
  An operation left mid-way by the old code before upgrading could still be applied twice.
- F03: payload carries `destination` and `outbound`; both recomputed when approving and
  before every new write. GitHub credentials and repository remain deployment-wide sandbox
  settings, not versioned tenant connections (Package 11).
- F04: local freshness only; a source-system change OpsBridge has not fetched is not seen.
  Investigations recorded before 0012 count as stale and must be re-run.
- F07: one legal entity and one purchasing source per tenant are assumed; credit-note
  numbering and multiple source systems are not modelled. Existing cases keep their
  previously normalized numbers. A replay is recognised by the Idempotency-Key and the
  request recorded when the case was created (0014); another key for an invoice that has a
  case is a duplicate. (A first version compared the request with the case's current
  fields; the browser tests caught that a case which had since learned its PO was then
  refused as a "reused key".)
- F11: only seven fields are correctable (those matching applies); other fields are read-only
  in the UI and refused by the API.
- F12: parsers remain time-bounded, not memory- or process-isolated (Package 9).

## Migrations and rollback

| Revision | Change | Downgrade |
|---|---|---|
| 0010 | Partial unique index: one succeeded local dispatch attempt per operation | Drops the index. Fails to upgrade if a database already holds a duplicate from the old code: review those operations first. |
| 0011 | `proposals.destination`, `proposals.outbound` | Drops both. Proposals created after 0011 then no longer match their digests and become stale (safe). |
| 0012 | `investigation_versions.evidence_state` | Drops it; the old code ignores freshness again. |
| 0013 | `cases.supplier_id`; current-invoice index per supplier | Restores the per-number index; fails if two suppliers' same-numbered invoices are current. |
| 0014 | `cases.intake_key`, `cases.intake_digest` (+ unique per requester and key) | Drops them; replays are then recognised only for manual cases. |

All five ran on the dev, test and eval databases; `alembic check` reports no drift. Backup and
restore drill at 0014: 7083 rows, counts equal, every table with forced RLS.
