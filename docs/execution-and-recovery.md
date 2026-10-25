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
| awaiting_review / follow_up_pending / investigating | resolved | investigator, **only** if the latest investigation outcome is `matched` and its evidence and effective policy are still current |
| any open state | closed_without_resolution | (reserved; not exposed in v1 UI) |

Creating a follow-up task never resolves a case.

**Proposal** (`proposals.status`): `awaiting_approval` → `approved` | `rejected` | `expired`
| `superseded`. Versions are immutable; an edit creates version n+1 and supersedes n.
A newer investigation, a policy change, or a failed dispatch precondition supersedes an
approved-but-undispatched proposal. Created/edited by `proposal.create` (investigators);
decided by `proposal.decide` with the action type in the approver's scope.

**Operation** (`operations.status`): `pending_dispatch` → `executing` → `confirmed` |
`failed` | `outcome_unknown`; or `pending_dispatch` / `executing` →
`cancelled_before_dispatch` when no write has landed and the next one is refused (an
`executing` operation gets there after a definite failure or an ambiguous one proved
absent).
`operations.cancellation` is separate: `none` | `requested` | `confirmed` | `unknown`.

**Attempt** (`attempts.status`): `started` → `succeeded` | `definite_failure` |
`ambiguous_failure`. Kinds: `dispatch`, `reconcile`, `readback`. Every attempt has its own
id, a `parent_attempt_id`, the stable `operation_key`, the worker id and fencing token.

## Approval binding

An approval (`approval_decisions`) records approver, tenant, proposal id and version, the
canonical payload digest (`opsbridge-cjson/1` over action, parameters, policy version,
investigation version, source-manifest digest, expiry, and since migration 0011 the
destination and the outgoing text), policy version, source-manifest
digest, scope, separation-of-duties result and its own expiry (policy
`approval.lifetime_minutes`; demo default 30 min furniture / 20 min IT — a product choice).
The approver must submit the version and digest they reviewed. Approval rows are
insert-only for the application role; consumption is written at insert time together
with the operation, outbox event and dispatch job (one transaction).

Destination and outgoing text. The proposal carries where the action goes (connector plus
simulator organization or GitHub repository; never credentials) and, for an external
action, the exact title and body that leave OpsBridge after redaction
(`opsbridge-outbound/1+opsbridge-redact/2`). The UI shows both before approval. Approving
and every dispatch recompute them from the current tenant settings, deployment
configuration, redaction rules and case identifiers; any difference makes the proposal
stale, so a settings change cannot redirect an approved action. Dispatch sends exactly the
approved text plus the marker line, and the attempt's request (its digest and kept copy)
includes the body and destination. Proposals stored before migration 0011 keep their
digests; an external one must be proposed again.

Evidence freshness. Each investigation records the evidence state before matching read it
(`services/freshness.py`): the versions matching would load, every version of the case's
linked documents, and the corrections on them. Creating a proposal, approving, every
dispatch and resolving recompute it; a correction, an upload, or a new version ingested for
another case makes the result stale until the investigation is re-run. Dispatch rechecks
under the case lock that corrections and uploads take. This digest is local freshness only:
it cannot show that the source system has not changed since the records were fetched. So
each investigation also records when it read the purchasing system, and its result may be
acted on for `source_max_age_hours` (24) after that; later, or when the read time was not
recorded (investigations before this change), it is stale and must be re-run. The case's
investigation view shows the read time and, when stale, why.

Source reads (`connectors/source_contract.py`, contract `purchasing-source/1`). Listings
follow the source's pages to the end; a failed page fails the step (retried), and a listing
longer than the connector's bound (50 pages of 100) sends the case to manual review instead
of concluding from part of it. A record the source no longer holds (reported deleted, a 404,
or absent from a complete listing for its PO) is marked withdrawn and its records are
superseded, so it stops counting; uploads are never withdrawn by a listing. A withdrawn
record that returns becomes current again as a new version.

## Dispatch (see `workflow/execution.py`)

1. Terminal operation → no-op (duplicate delivery).
2. A `started` attempt younger than the lease window from another lease → back off.
   An abandoned `started` attempt or an `ambiguous_failure` → reconcile first; a write that
   may have landed is recovered (reconciliation, read-back) even if the approval has since
   expired, because it was authorized when it was made.
3. Before every NEW write (the first dispatch, a retry after a definite failure, a create
   after reconciliation proved an earlier write absent): cancellation requested →
   `cancelled_before_dispatch` (cancellation confirmed); otherwise re-validate: proposal
   still `approved` and digest intact, approval unexpired, approver still holds
   `proposal.decide` + scope (membership read now), evidence and effective policy current,
   destination and outgoing text unchanged. Any failure → cancelled, no call. (Until
   2026-10-22 this ran only while the operation was `pending_dispatch`, so a retry from
   `executing` skipped it; review finding F01.)
4. Commit a `started` attempt, call the connector outside any transaction, record the
   result, then read the task back before marking `confirmed`.
5. Local actions (review note, supplier draft, case assignment) apply the effect, record a
   succeeded attempt, write the outbox event and confirm the operation in ONE transaction,
   after the same eligibility check; a unique index allows one applied local action per
   operation (migration 0010). Before 2026-10-22 these committed separately and a crash in
   between duplicated the note or draft on retry (F02).

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
| A retry needs current eligibility: definite or proven-absent failure × expiry, revocation, cancellation, edit, policy change → no second create | `test_a_retry_rechecks_eligibility_before_any_new_create[*]` (10) |
| A write that landed is still confirmed after the approval expires | `test_a_create_that_landed_is_confirmed_even_after_the_approval_expires` |
| A worker dying while confirming a local draft leaves nothing; the retry writes one; the database refuses a second | `test_a_local_action_takes_effect_once_even_if_the_worker_dies_while_completing` |
| Settings change after drafting cannot redirect the approval | `test_a_settings_change_after_drafting_cannot_redirect_the_approval` |
| Destination or outgoing text changed after approval → no dispatch | `test_dispatch_refuses_when_destination_or_outgoing_text_changed_after_approval[connector/redaction]` |
| Correction, upload or another case's new source version → stale until re-run; correction during dispatch is seen | `tests/integration/test_evidence_freshness.py` (7) |
| Remote stops honouring idempotency keys, no lookup → review, one task | `test_remote_that_stops_honouring_idempotency_goes_to_review_not_a_duplicate`, `test_unreliable_remote_behind_the_mcp_hop_goes_to_review_not_a_duplicate` |
| Capability discovery; failed discovery means weakest | `test_capabilities_are_discovered_and_unknown_means_weakest` |
| UI: reconcile after timeout; unknown outcome recorded by a person | `frontend/e2e/demo.spec.ts` |

## Residual risks (stated, not solved)

- Delivery is at-least-once; there is no universal exactly-once guarantee.
- After revalidation, a remote call is in flight; revocation or a correction during that
  window cannot stop it. Leases must exceed connector timeouts (enforced: lease ≥ 2 × timeout) so a slow
  worker is not mistaken for a crashed one; idempotency keys cover the remaining window
  for the simulator connector.
- Read-back confirms the task exists with our marker; it does not confirm anyone acted on it.
- Local cancellation cannot undo a dispatched external action.
- Capabilities can change between discovery and the call; a provider that advertises
  idempotency but does not honour it can still cause a duplicate on resend. Production
  connectors need validation against the real provider (GitHub declares no idempotency).
