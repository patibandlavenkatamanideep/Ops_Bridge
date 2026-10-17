# Approvals, execution and recovery

Status: implemented and verified locally against the purchasing/task simulator over
real HTTP with fault injection (`tests/integration/test_approvals_execution.py`,
`frontend/e2e/phase4.spec.ts`). The GitHub connector (Phase 5) uses the same executor.

## State machines (separate fields, never one overloaded status)

**Case** (`cases.status`; transitions in `services/cases.py::CASE_TRANSITIONS`)

| From | To | Triggered by |
|---|---|---|
| new / needs_information / awaiting_review / follow_up_pending | collecting_evidence | investigation run start (investigator) |
| collecting_evidence | investigating | worker, after evidence collection |
| investigating | needs_information / awaiting_review | worker, from the deterministic outcome |
| awaiting_review | follow_up_pending | worker, when an approved non-note operation is confirmed |
| awaiting_review / follow_up_pending / investigating | resolved | investigator, **only** if the latest investigation outcome is `matched` |
| any open state | closed_without_resolution | (reserved; not exposed in v1 UI) |

Creating a follow-up task never resolves a case.

**Proposal** (`proposals.status`): `awaiting_approval` → `approved` | `rejected` | `expired`
| `superseded`. Versions are immutable; an edit creates version n+1 and supersedes n.
A newer investigation, a policy change, or a failed dispatch precondition supersedes an
approved-but-undispatched proposal. Created/edited by `proposal.create` (investigators);
decided by `proposal.decide` with the action type in the approver's scope.

**Operation** (`operations.status`): `pending_dispatch` → `executing` → `confirmed` |
`failed` | `outcome_unknown`; or `pending_dispatch` → `cancelled_before_dispatch`.
`operations.cancellation` is separate: `none` | `requested` | `confirmed` | `unknown`.

**Attempt** (`attempts.status`): `started` → `succeeded` | `definite_failure` |
`ambiguous_failure`. Kinds: `dispatch`, `reconcile`, `readback`. Every attempt has its own
id, a `parent_attempt_id`, the stable `operation_key`, the worker id and fencing token.

## Approval binding

An approval (`approval_decisions`) records approver, tenant, proposal id and version, the
canonical payload digest (`opsbridge-cjson/1` over action, parameters, policy version,
investigation version, source-manifest digest, expiry), policy version, source-manifest
digest, scope, separation-of-duties result and its own expiry (policy
`approval.lifetime_minutes`; demo default 30 min furniture / 20 min IT — a product choice).
The approver must submit the version and digest they reviewed. Approval rows are
insert-only for the application role; consumption is written at insert time together
with the operation, outbox event and dispatch job (one transaction).

## Dispatch (see `workflow/execution.py`)

1. Terminal operation → no-op (duplicate delivery).
2. Cancellation requested before dispatch → `cancelled_before_dispatch`.
3. Re-validate before the first remote call: proposal still `approved` and digest intact,
   approval unexpired, approver still holds `proposal.decide` + scope (membership read now),
   evidence manifest and effective policy unchanged. Any failure → cancelled, no call.
4. A `started` attempt younger than the lease window from another lease → back off.
   An abandoned `started` attempt or an `ambiguous_failure` → reconcile first.
5. Commit a `started` attempt, call the connector outside any transaction, record the
   result, then read the task back before marking `confirmed`.

Connector declarations (`connectors/tasks.py`):

| Connector | Idempotency keys | Lookup | Conditional writes | Definite | Ambiguous |
|---|---|---|---|---|---|
| sim_tasks | discovered (`GET /orgs/{org}/capabilities`) | discovered: strong (marker search) or none | no | 400/401/403/404/409/422; 503 + `X-Sim-Not-Committed` | timeouts, dropped connections, other 5xx |
| github_issues | no | eventual | no | 400/401/403/404/410/422 | timeouts, 5xx |

Capabilities are a contract the executor relies on. The simulator connector discovers them
from the tracker for each dispatch unless they are configured explicitly; if discovery fails
it assumes the weakest contract (no idempotency, no lookup), so an ambiguous result goes to
human review. (Before Phase 9 the simulator connector hard-coded "idempotency: yes"; a demo
test showed that a tracker which silently stopped honouring keys, with lookup unavailable,
received a resend after a timeout and created a second task. Fixed and covered by
`test_remote_that_stops_honouring_idempotency_goes_to_review_not_a_duplicate` and the same
scenario through Casework MCP.)

Reconciliation: strong lookup with one match → confirmed; none → safe to create; several →
`outcome_unknown` (duplicates exist). Without lookup, idempotency keys make a resend safe.
Without either → `outcome_unknown` + review. An inconclusive lookup is never treated as
absence.

## What the tests demonstrate

| Scenario | Test |
|---|---|
| Approved task created once, read back, case stays open | `test_approved_follow_up_is_created_once_confirmed_and_case_stays_open` |
| Self-approval rejected (SoD) | `test_self_approval_is_rejected_by_separation_of_duties` |
| Scope / role / admin cannot approve | `test_approver_scope_and_role_are_enforced` |
| Must present reviewed version + digest | `test_approval_must_present_the_reviewed_version_and_digest` |
| Payload edited after approval | `test_payload_edited_after_approval_cannot_be_dispatched` |
| Source change before dispatch | `test_source_change_before_dispatch_rejects_the_approval` |
| Approver revoked / approval expired | `test_revoked_approver_or_expired_approval_cannot_dispatch` |
| Duplicate delivery | `test_duplicate_delivery_after_completion_is_a_no_op` |
| Two concurrent workers | `test_two_concurrent_workers_produce_one_operation_and_one_task` |
| Stale worker after lease loss | `test_stale_worker_cannot_write_after_losing_its_lease` |
| Timeout after commit → reconcile | `test_timeout_after_commit_reconciles_instead_of_recreating` |
| No idempotency, no lookup → unknown + review | `test_no_idempotency_and_no_lookup_yields_outcome_unknown_and_review` |
| Ambiguous 500 before commit | `test_ambiguous_500_before_commit_is_reconciled_as_absent_then_retried` |
| Definite retryable / non-retryable | `test_definite_failures_retry_or_fail_without_duplicates` |
| Crash before / after remote commit | `test_worker_crash_before_or_after_remote_commit_recovers_without_duplicate[before/after]` |
| Cancellation before dispatch / too late | `test_cancellation_before_dispatch_and_too_late` |
| Local actions use the same ledger | `test_local_review_note_runs_through_the_same_ledger` |
| Remote stops honouring idempotency keys, no lookup → review, one task | `test_remote_that_stops_honouring_idempotency_goes_to_review_not_a_duplicate`, `test_unreliable_remote_behind_the_mcp_hop_goes_to_review_not_a_duplicate` |
| Capability discovery; failed discovery means weakest | `test_capabilities_are_discovered_and_unknown_means_weakest` |
| UI: reconcile after timeout; unknown outcome recorded by a person | `frontend/e2e/demo.spec.ts` |

## Residual risks (stated, not solved)

- Delivery is at-least-once; there is no universal exactly-once guarantee.
- After revalidation, a remote call is in flight; revocation during that window cannot stop
  it. Leases must exceed connector timeouts (enforced: lease ≥ 2 × timeout) so a slow
  worker is not mistaken for a crashed one; idempotency keys cover the remaining window
  for the simulator connector.
- Read-back confirms the task exists with our marker; it does not confirm anyone acted on it.
- Local cancellation cannot undo a dispatched external action.
- Capabilities can change between discovery and the call; a provider that advertises
  idempotency but does not honour it can still cause a duplicate on resend. Production
  connectors need validation against the real provider (GitHub declares no idempotency).
