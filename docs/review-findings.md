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
| F05 | P1 | Investigation jobs lack fencing and renewal | Fixed, verified locally | `tests/integration/test_investigation_leases.py` (4: a slow step keeps its job; a stuck step loses it and its late write is refused; a job whose worker keeps dying is given up; a resumed run keeps its model-call count) |
| F06 | P1 | Spend and quotas are check-then-act | Fixed, verified locally | `tests/integration/test_spend_reservations.py` (4: 10 concurrent calls with funds for 3 → 3 sent; daily quota 3 → 3; a crashed or timed-out call stays charged; 10 concurrent starts with an hourly quota of 3 → 3), `tests/agents/test_spend_settings.py` |
| F07 | P1 | Invoice identity tenant-wide, destructive normalization | Fixed for supplier scope, look-alikes and idempotency; verified locally | `tests/integration/test_business_identity.py` (3) |
| F08 | P1 | Retention deletes shared bytes | Fixed, verified locally | `tests/integration/test_retention_blobs.py` (3: shared bytes kept; bytes go only after the rows commit and a crash is finished later, even with a re-upload in between; a storage failure stays queued) |
| F09 | P1 | Unverified cookies select rate-limit buckets | Fixed, verified locally | `test_made_up_cookies_do_not_escape_the_address_limit`, `tests/api/test_perimeter.py` (trusted proxies, bounded fallback) |
| F10 | P1 | Unit inverse accepts a rounded reciprocal | Fixed, verified locally | `tests/domain/test_units.py` (19 incl. a property test) |
| F11 | P1 | Accepted corrections never applied | Fixed, verified locally | `test_only_fields_matching_uses_can_be_corrected` |
| F12 | P1 | Unreadable replacement leaves old records current | Fixed, verified locally | `test_an_unreadable_replacement_invoice_is_not_matched_on_the_old_extraction[*]` (3) |
| F13 | P1 | Production configuration may keep development defaults | Fixed (settings checks), verified locally | `tests/test_deployment_settings.py` (16) |
| F14 | P1 | `main` unprotected; CI does not qualify the runtime image | In-repository part done; branch protection and publishing remain the owner's | CI runs on pushes to `main`; API-type drift step; image job (build, smoke, SBOM, scan) — first run on PR #4; base images pinned by digest; Dependabot config |
| F15 | P2 | Validators check numbers and citations, not claim meaning | Partly fixed, verified locally | role and unit checks: `test_compromised_explanations_are_rejected[…stated as invoiced / uses unit SET]`; no false alarms on any corpus finding summary (`test_finding_numbers.py`); 0 of 120 recorded held-out mock explanations newly rejected |
| F16 | P2 | Local artifact reads do not verify digests | Fixed, verified locally | `tests/evidence/test_local_store.py`, `test_an_altered_original_is_refused_not_served` |
| F17 | P2 | `verify` can pass with required gates skipped | Fixed, verified locally | `make verify-strict` fails here on the skipped OCR tests (tesseract not installed), as intended |
| F18 | P2 | UX needs operator testing | Partly: destination and exact outgoing text shown before approval; Correct offered only for correctable fields; supplier and look-alike confirmation at intake; the server states each viewer's decision reason; expiry noticed while the page is open; queue search debounced; README screenshots fixed | `test_the_server_says_who_may_decide_a_proposal_and_why_not`; browser suite and accessibility scan pass |

Found while building the source contract (not in the review):

| ID | Finding | Status | Regression evidence |
|---|---|---|---|
| X5 | A PDF under 1 MB whose streams inflate to text made the parser subprocess use about 850 MB until the 10 s timeout stopped it; concurrent uploads multiplied that | Fixed (Linux), verified in the app image | The worker caps its address space at 256 MB (a legitimate 50-page, 3.9 MB-text PDF parses in 1.9 s under it; the bomb is rejected in 2.2 s) and at most two PDF parses run at once per process: `test_a_pdf_inflating_past_the_memory_cap_is_rejected_before_the_timeout` (Linux only), `test_no_more_than_two_pdfs_are_parsed_at_once` |
| X4 | JSON nested about a thousand levels deep made `json.loads` raise RecursionError: an upload or source record failed with an unexpected error instead of a rejection, a source listing escaped the connector's error handling, a model answer failed its step instead of being rejected | Fixed, verified locally | Depth counted before decoding (`safejson`): `test_json_limits_and_exact_numbers`, `test_model_output_nested_beyond_any_schema_is_rejected_not_raised`, `test_a_listing_nested_beyond_any_record_is_refused` (all raised RecursionError before), `tests/test_safejson.py`; API bodies were already refused with 400 by the framework (guard test) |
| X3 | After a create timed out, a strong lookup that found nothing was taken as proof of absence, so a create the tracker committed after the client gave up landed next to the resend (no idempotency keys: two tasks) | Fixed, verified locally | `test_a_create_still_in_flight_is_not_proved_absent_and_sent_again[True]` (failed before: 2 tasks) and `[False]` (a tracker without deadlines: review, one task) |
| X2 | Dispatch built its connector from the configuration at the moment it ran, so recovering a write that had already landed (worker died after the create, or a retried read-back) looked wherever the configuration pointed by then | Fixed with tenant task connections, verified locally | Probe on the previous code: GitHub issue created, worker dies, repository setting changed → `outcome_unknown` (unreconcilable) though the issue existed; unchanged configuration → confirmed. Now `test_recovery_after_the_connection_moved_looks_where_the_write_went` |
| X1 | The policy date came from any current invoice with the number (another supplier's older invoice could put the case under a superseded policy), and proposal checks took the newest policy, even a future-dated one, when no single invoice date applied (such a case could never get a proposal) | Fixed, verified locally | `test_the_policy_is_chosen_by_this_suppliers_invoice_date`, `test_a_case_without_one_invoice_date_is_judged_under_todays_policy` |

## Package 11, local part: the purchasing-source contract (2026-10-25)

The review asked for one real read-only source adapter and, without access to one, for the
contract harness with the blocker recorded. No customer purchasing system or export has been
chosen, so no adapter for a real system exists and nothing here is live-validated.

| Gap | Before | Now | Regression evidence |
|---|---|---|---|
| Pagination | One response per listing: a source that pages lost every receipt after page one | Every page is read; a failed page fails the step; a non-advancing cursor is refused | `test_every_page_of_a_listing_is_read` (failed before: 6 of 10 received) |
| Completeness | No notion of a partial listing | Past the bound (50 pages of 100) the listing says it is incomplete and the case goes to manual review (`source_listing_incomplete`) | `test_a_listing_cut_off_at_the_page_bound_is_not_concluded` |
| Withdrawals (tombstones) | A receipt deleted, voided or moved to another PO in the source stayed current and kept counting | Reported tombstones, 404s and absence from a complete listing mark the document withdrawn (0016) and supersede its records; uploads are never withdrawn; a returning record is current again | `test_a_receipt_withdrawn_in_the_source_no_longer_counts[deleted, moved_to_another_po]` (failed before: still matched), `test_an_uploaded_receipt_is_not_withdrawn_by_the_source` |
| Source freshness | Nothing recorded when the source was read | Each investigation records the read time; its result may be acted on for `source_max_age_hours` (24); the case shows the read time and an "Out of date" reason | `test_purchasing_data_older_than_the_limit_cannot_be_acted_on` (failed before: approved) |
| Tenant task connections | GitHub repository and token were deployment-wide; the connector was a tenant setting with no history; no way to stop a tenant's external writes | Versioned per-tenant connection (0017): destination, credential reference to the secret store, reason, who and when; repositories on a deployment allowlist; rotate, move, revoke; dispatch writes and recovers at the approved destination; a revoked credential is never used again | `tests/integration/test_task_connections.py` (4), updated connector tests, browser test for revoke and restore |
| Contract harness | none | `connectors/source_contract.py` (`purchasing-source/1`) and `tests/contracts/test_purchasing_source_contract.py` (21: paging, tombstones, moved records, credentials errors are not "not found", outages, bound, mid-listing failure, non-advancing cursor, malformed, redirect, oversize, timeout, key validation) | runs in the unit suite |

Not covered:

- No real system. A real adapter must pass the contract suite against its sandbox, plus that
  system's own pagination, rate-limit and change-timestamp behaviour. Blocker: the owner has
  to choose a customer system or export and provide a sandbox and authorization.
- Reads stay per case, on demand: there is no background sync, change feed or cursor kept
  between investigations. Freshness is bounded by the age limit, not by change notification.
- Withdrawal is inferred only for records filed against the PO being read; a record moved
  between POs is current again once a case for its new PO reads it.
- Task connections: the secret store is environment variables
  (`OPSBRIDGE_CONNECTION_SECRET_<REF>`); a deployment would map them from its secret manager.
  One active connection per tenant. GitHub live writes are not live-validated.
- Observed once in three full integration runs (never in eight isolated runs):
  `test_timeout_after_commit_behind_the_mcp_hop_is_reconciled` logged "ambiguous failure
  reconciled as absent" and failed. Behind the MCP hop a timed-out create may still be in
  flight when the lookup runs. Without idempotency keys that was a real duplicate risk (X3,
  fixed with request deadlines, also through the hop). With keys, as in that test, the resend
  is safe and the task is then confirmed by read-back instead of by marker, which is what the
  test's method assertion rejects: a timing-dependent test, not a duplicate. Left as is.
- Also once under load (an integration run while the dev stack and a browser run were busy;
  not in the next full gate, not in isolation): `test_denied_records_calls[forged_delegation]`
  failed. Its output was not kept, so the cause is not known; a transport timeout instead of
  the expected denial is the likely one. Watch for it in CI.

## What changed, and what it does not cover

- F01: every new write (first dispatch, retry after a definite failure, create after
  reconciliation proved absence) passes cancellation and full re-validation; recovering a
  write that may have landed does not. Not covered: the requester's own permissions are
  not re-checked (the policy names no requester permission).
- F02: local actions commit effect, succeeded attempt, outbox event and confirmation
  together; a partial unique index (0010) allows one applied local action per operation.
  An operation left mid-way by the old code before upgrading could still be applied twice.
- F03: payload carries `destination` and `outbound`; both recomputed when approving and
  before every new write. Since 0017 the destination comes from the tenant's versioned task
  connection, and dispatch builds its connector from the approved destination.
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
- F12: parsers are time-bounded; PDF parsing is also memory-capped (Package 9, below).
- F05: a step may renew its lease for `investigation_step_limit_seconds` (default 300);
  after that the worker stops looking alive and another may take the run, whose late writes
  from the first are refused. A model call already in flight on the stuck worker still
  happens and is recorded (it is a fact), but cannot change the run's result. The benchmark
  harness runs without a lease.
- F06: the ceiling is per organization (lifetime of the recorded calls), the quota per
  organization and UTC day. A reservation is never released, only settled: the estimate
  stays charged when no usage comes back. The estimate (3 characters per token plus the
  full output allowance) is not a guaranteed bound. Investigation starts in an organization
  are serialized by a transaction lock.
- F08: bytes are deleted only when no live artifact references them; backups and S3
  noncurrent versions still hold them (docs/privacy-and-retention.md).
- F09: behind a load balancer `trusted_proxies` must name it, or every client is counted
  under its address; the per-address default is 1200 requests/min.
- F10: an inverse conversion exists only when it terminates; policies refuse zero, negative
  or non-finite factors, contradictory pairs and aliases naming two units.
- F16: an altered original is refused (`evidence_integrity_failed`, logged); the next store
  of the same content rewrites the file.
- F13: `staging` (synthetic data, simulator allowed) and `production` refuse the local
  artifact store, unencrypted S3, disabled quotas, a per-process rate limit and missing trusted
  proxies; `production` refuses to start at all until a production purchasing adapter exists.
  These are settings checks, not proof of the deployed controls.
- F14: the scan found fixable advisories only in Pillow 12.3.0 and certifi 2026.7.22; both
  were updated (13.0.0, 2026.10.19). The rebuilt image could not be rescanned locally (host
  disk nearly full); the CI image job is the first scan of it. Publishing, signing,
  provenance and branch protection are not done.
- F17: `verify --strict` is the release gate; the default `verify` stays a convenience
  report that now names every skipped gate and test.
- F15: a number next to a role word (invoiced, ordered, received, returned, allocated,
  claimed) must equal a finding value of that role, and a number followed by a unit must use
  the findings' unit. Not checked: comparisons ("above"/"below"), dates, subjects beyond
  these roles, prose without numbers. Structured claim-to-finding output would need a new
  prompt version and therefore a new benchmark (the frozen recordings would no longer
  replay); not done.
- F18, queue ownership and views (2026-10-25): a case can be taken, handed to another
  investigator or released (`POST /cases/{id}/owner`, refused when the owner changed since
  the caller looked; audited); the queue has views *Mine*, *Unassigned* (including owners
  who left), *Awaiting approval* and *Needs review* (the recovery view for unknown
  outcomes) with counts, oldest-first order, and keeps view, filters and order in the URL.
  Before, nothing could set a case's owner. Tests: `tests/integration/test_case_ownership.py`
  (5), a queue unit test, `e2e/queue.spec.ts`.
- F18 not done: due dates (invoices carry no payment terms in the packs; an SLA would be a
  policy decision), saved views per person beyond URLs, splitting the tenant settings page,
  runtime validation of successful API responses, and usability sessions with operators.
- Observed while checking F15: the benchmark's mock trials depend on the `rules_only` trials
  having run first on the same cases (a replay of `mock` alone diverges); the frozen run order
  always does so. Recorded as a harness property, not changed.

## Migrations and rollback

| Revision | Change | Downgrade |
|---|---|---|
| 0010 | Partial unique index: one succeeded local dispatch attempt per operation | Drops the index. Fails to upgrade if a database already holds a duplicate from the old code: review those operations first. |
| 0011 | `proposals.destination`, `proposals.outbound` | Drops both. Proposals created after 0011 then no longer match their digests and become stale (safe). |
| 0012 | `investigation_versions.evidence_state` | Drops it; the old code ignores freshness again. |
| 0013 | `cases.supplier_id`; current-invoice index per supplier | Restores the per-number index; fails if two suppliers' same-numbered invoices are current. |
| 0014 | `cases.intake_key`, `cases.intake_digest` (+ unique per requester and key) | Drops them; replays are then recognised only for manual cases. |
| 0015 | `blob_deletions` (forced RLS, worker only) | Drops the queue; pending deletions are then forgotten (bytes stay: safe). |
| 0018 | `attempts.deadline_at` | Drops it; absence after a timeout can then no longer be proved for a provider without idempotency keys (such operations go to review: safe). |
| 0017 | `task_connections` (forced RLS; version 1 per tenant from its connector setting) | Drops it; the connector is read from `tenant_settings.task_connector` again (kept in step), the GitHub repository and token from deployment settings. A GitHub tenant starts 0017 with no repository and must set one. |
| 0016 | `source_documents.withdrawn_at` | Drops it; withdrawn documents' records stay superseded, and an unchanged record that returns is not reinstated until its bytes change: re-ingest it, or re-upgrade. |

All nine ran on the dev, test and eval databases; `alembic check` reports no drift. Backup and
restore drill at 0015: 7465 rows, counts equal, every table with forced RLS.
