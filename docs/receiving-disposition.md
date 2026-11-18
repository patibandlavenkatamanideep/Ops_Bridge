# Receiving and disposition

What OpsBridge records about a delivery after it arrives, how it derives delivered, accepted
and rejected quantities, what it asks next, and what an approval may still do once receiving
evidence changes. Everything here was built and tested on synthetic data
(`fixtures/receiving/disposition.yaml`). No customer receiving data has been read, and no
procurement reviewer has checked these rules.

Code: `domain/receiving.py` (positions), `domain/matching.py` (rules `opsbridge-rules/1.4.0`),
`domain/decision.py` (routes), `mapping.py` and `packs/furniture/pack.yaml` (import),
`persistence/migrations/versions/0025_receiving_events.py`, `services/receiving.py` and the
case page's **Receiving** tab.

## 1. Before this change

Run at commit `344225d` (rules `opsbridge-rules/1.3.0`, workflow `investigation/1.4.0`). Every
case bills 20 EA on one PO line ordered at 20. The new tests in
`backend/tests/domain/test_receiving.py` were run against the old rules first: 26 failed and 4
passed. The 4 were the controls: the 1.3.0 replay, the new-order negative control, over-billing
without a replacement, and the absence of any payment action.

| Source facts | Stored facts | Investigation result | Evidence and next question | Effect on an approval |
|---|---|---|---|---|
| A: 18 arrive, 18 accepted | receipt line 18 accepted | quantity mismatch: "only 18 were received" | cites the 18; receiving: "Confirm delivered quantity" | n/a |
| B: 20 arrive, 18 accepted, 2 rejected as damaged | 18 accepted, 2 rejected; **the reason has no field** | quantity mismatch, rejected units counted | cites the rejected row; **the same question as A**, to receiving | n/a |
| A receipt for 20 accepted, then a separate rejection of 2 | two receipts: 20 accepted, 2 rejected | **matched** | nothing about the rejection | the approval is blocked (the evidence changed), but the new result is "matched" again |
| B, then the 2 rejected chairs are returned | 18 accepted, 2 rejected, a return of 2 | **16 received, "4 EA exceed"** | the rejected units are subtracted twice | n/a |
| An export with a received quantity and no status (a pack that maps none, or marks it optional) | **status `accepted`** (the mapping's default) | as if accepted | none | n/a |
| A corrected receipt (8, then 10) after approval | a new version; the old one superseded | the new facts | both versions can be opened | blocked; returns for review (already worked) |
| The rejection arrives after the follow-up was sent | the new record | the new result | none | **nothing said the sent action rested on changed facts** |

First layer where information was lost:
- **The rejection reason:** the canonical record (`ReceiptLine` had no field).
- **A late rejection:** the rules counted it beside the receipt instead of against it.
- **A return:** the rules subtracted the same units twice.
- **A generic "received" quantity:** the mapping defaulted the status to accepted.

Credits, replacements and promises had no representation at all.

Three things already worked:
- **Versions:** an identical reimport stores nothing new, and a changed source record becomes
  a new version, with the old one kept and superseded.
- **Stale dispatch:** an approval whose evidence changed is refused by the worker and the
  casework tool, and the proposal is superseded.
- **The supplier's claim:** a claim of "all 20 delivered" is never receipt evidence.

## 2. Facts and how they are stored

A **receipt line** (`ReceiptLine`, table `receipt_lines`) is one movement:
- `kind` is `receipt` or `return`;
- `status` is `accepted`, `rejected`, `reversed` or `unstated`;
- `reason` is the source's rejection reason, if given (migration 0025).

`unstated` means the source gives a received quantity without saying whether it was accepted.
Such units are delivered, and are never counted as accepted.

A **receiving event** (`ReceivingEvent`, table `receiving_events`) is a fact recorded after a
delivery. Each event has:
- a stable `event_id`;
- the PO line it concerns;
- a quantity and unit, where the event type has one;
- what it refers to;
- `occurred`, as the source states it.

The event types:

| Event | Refers to | Effect |
|---|---|---|
| `rejection` | the receipt it inspects (`receipt_number`) | units move from accepted to rejected on that receipt |
| `return` | a rejection, or the receipt whose rejected units it concerns | physical return; resolves nothing by itself |
| `replacement_promised` | a rejection or receipt | a promise; resolves nothing |
| `replacement_delivery` | a rejection, a promise, or a receipt | replacements arrived; acceptance still to come |
| `replacement_acceptance` | the replacement delivery (`follows`) | replacement units count as accepted |
| `credit_promised` | a rejection, return or receipt | a promise; resolves nothing |
| `credit_posted` | as above, plus the invoice (`invoice_number`) and credit document (`document_number`) | resolves units only for the invoice it names |

Time:
- `occurred` is kept as given: a date (`2026-10-20`), an RFC 3339 time with a zone, or NULL
  when unknown. Anything else is refused at import.
- When OpsBridge learned of the event is the stored record's `created_at`.
- So a rejection dated before an approval and recorded after it shows as "learned after
  approval".

Corrections and events are different things:
- **A correction** is a new version of the same source record: same key, new bytes. The old
  version and its records stay, superseded.
- **A late rejection** is its own record, linked to the receipt it rejects.
- An edited export never counts as proof that a separate return or rejection occurred: a
  changed row is a revision of that row.

Records read before 0025 keep their status and have no reason. The migration infers nothing:
it creates no event and fills no reason (`tests/integration/test_migration_0025.py`).

## 3. Import (`packs/furniture/pack.yaml`)

Source field names stay in the pack.

- **Receipts** (CSV, one row per movement) gain an optional `reason` column. A receipt exported
  without it gets no reason, and nothing else about it changes.
- **A receipt row with no status** is the one compatibility change. It used to be read as
  `accepted`, and is now `unstated`, so the case asks for the acceptance.
  - Both shipped packs require the status, and refuse a blank one, as before.
  - A pack that marks the status optional, or maps none, gets the new behaviour.
  - A pack can state `default: accepted`, which is then cited as the pack's choice.
  - Which exports change, and what happens to records already stored:
    docs/pilot/export-contract.md, "How receipts count".
- **Receiving events** (CSV, collection `receiving_events`, filed under the PO, one row per
  event):

| Column | Required | Meaning |
|---|---|---|
| `event_id` | yes | stable identity within the tenant |
| `event` | yes | `REJECTED`, `RETURNED`, `REPLACEMENT_PROMISED`, `REPLACEMENT_DELIVERED`, `REPLACEMENT_ACCEPTED`, `CREDIT_PROMISED`, `CREDIT_POSTED` |
| `po_ref`, `po_line` | yes | the PO line |
| `qty`, `uom` | for rejections, returns, replacement deliveries and acceptances, posted credits | in a unit the policy knows |
| `occurred_on` | no | date or zoned time; blank is unknown, not today |
| `receipt_id` | for rejections; or for a remedy without `follows` | the receipt concerned |
| `follows` | no | the event this one follows |
| `invoice`, `document` | for posted credits | the invoice credited and the credit document |
| `reason` | no | why units were rejected |

Every value read is cited by its row and column (`csv:row=3;col=reason`). The `it_hardware` pack
has no receiving-event mapping.

Repeat, correction, order and tenant:
- **Repeat:** the same bytes again store nothing new.
- **Correction:** the same event in a changed row becomes a new version, and only the newest
  is counted (`test_a_corrected_event_row_is_a_new_revision_and_counts_once`).
- **Identity:** the same event id twice in one document is used neither time.
- **Order:** an event whose receipt or preceding event has not arrived is stored but not
  applied. The case says which record it waits for, and the event applies when that record
  arrives.
- **Tenant:** an event never attaches to another tenant's receipt (row-level security), and
  never to a guessed line.

`export-check` lists the events it read and notes each event the export cannot link.

A supplier email saying "everything arrived" stays a supplier assertion. It is context and
never stands in for acceptance.

## 4. Derived quantities, per PO line, in its unit

- **delivered** = receipt lines of kind `receipt` that were not reversed (accepted, rejected or
  unstated). It is unknown without a receipt.
- **accepted** = accepted receipt lines, less units later rejection events move to rejected. It
  is unknown if any delivered units have unstated acceptance.
- **rejected** = rejected receipt lines plus rejection events. Each unit is counted once.
- **returned after acceptance** = `RETURN` movements on receipts.
- **covered** = accepted − returned after acceptance + accepted replacements. This is what the
  line can be billed for.
- **The order** is never increased by a replacement, and the rejection it replaces stays in
  the record.
- **Remedies** are tracked apart from movement: returned, replacement promised, delivered and
  accepted, credit promised, credit posted for this invoice, and credits posted elsewhere.
- **open rejected** = rejected − credit posted for this invoice − accepted replacements.

The following are refused, and the line's quantities are not concluded:
- a missing or unconvertible unit;
- a negative quantity;
- a reference that does not resolve, or points at the wrong kind of event or another line;
- a remedy exceeding what it refers to (for example, returning 3 of 2 rejected, or rejecting
  25 of a receipt that accepted 20).

The case goes to review, or asks for the missing record. It never becomes a clean match.

## 5. What rules 1.4.0 conclude, and whom they ask

The shortfall on a line (billed − covered − earlier allocations) is split into units that are
delivered and rejected (open) and the rest.

| Situation | Finding | Next step (furniture-ap@v2) |
|---|---|---|
| Short delivery | `quantity_mismatch` / `invoiced_exceeds_received`, with delivered and undelivered | receiving: "Confirm delivered quantity" |
| Rejected, no reason recorded | `disposition` / `rejection_reason_missing` (needs information) | receiving: the inspection record |
| Rejected with a reason, no remedy | `disposition` / `disposition_unrecorded` | the policy's disposition owner |
| Returned, no credit | `disposition` / `return_credit_unrecorded` | disposition owner: ask for the credit note |
| Credit promised, not posted | `disposition` / `credit_pending` | disposition owner: "a promise is not a credit" |
| Replacement promised, not arrived | `disposition` / `replacement_pending` | disposition owner |
| Replacement arrived, acceptance not recorded | `disposition` / `replacement_acceptance_unrecorded` (needs information) | receiving |
| Credited or replaced | note `rejected_units_credited` / `_replaced`; matched if nothing else | review note |
| A later invoice bills a replaced line already billed in full | `possible_duplicate_replacement_charge` (needs review) | AP review. Same item and quantity on another order is not flagged |
| An event waits for its receipt | `missing_receipt` / `event_reference_unresolved` (needs information) | receiving: the named record |
| Received quantity with no acceptance status | `missing_receipt` / `acceptance_not_stated` (needs information) | receiving |

Ownership:
- The disposition owner is the policy's `disposition` route (`action_routes.disposition`).
- **Without one** (furniture-ap@v2, the demo tenant's policy), the case goes to manual review.
  It says: "Policy furniture-ap@v2 names no owner for rejected goods, so OpsBridge does not
  choose whom to ask."
- That is a policy decision for the organization. Publishing an owner means a new policy
  version, and the frozen benchmarks bind the existing versions by digest.
- **The default stays unset on purpose.** Until an organization names an owner and a hold
  scope, manual review is the right outcome. The scenarios behind this work came from public
  discussion of receiving practice, which does not establish any company's policy.
- **Routing to a named owner** is shown with a labelled synthetic version,
  `fixtures/receiving/furniture-ap-v3-synthetic.yaml`. It is furniture-ap@v2 plus a
  `disposition` route to `procurement` and `disputed_quantity_hold: disputed_portion`.
  - It lives outside `packs/`, so `make seed-demo` never loads it.
  - A unit test checks it differs from v2 only by those two additions.
  - The unit tests use it as `CONFIGURED`.
  - `test_with_the_synthetic_policy_version_rejected_goods_go_to_its_named_owner` publishes it
    in a test tenant. Case B then goes to procurement with "Resolve rejected units with the
    supplier for PO-…", and its split is 18 matched and 2 disputed.

Payment:
- A note gives the matched and disputed quantities whenever a quantity is disputed:
  - `disputed_quantity_hold: whole_invoice`: the whole invoice is held;
  - `disputed_portion`: only the disputed portion is reviewed;
  - absent: the split is shown and the payment recommendation is left unresolved.
- Amounts are not computed from quantities.
- No action type can pay, short-pay, post a credit or change a source system
  (`test_no_action_type_can_pay_short_pay_or_post_a_credit`).
- An optional model may explain the findings or draft a follow-up. It cannot add a
  disposition, an owner or a quantity: drafts are validated against these findings.

## 6. Approvals and execution when receiving evidence changes

These cases reuse the existing approval, evidence-version and admission mechanisms
(`services/execution_admission.py`, `services/freshness.py`). There is no second approval
system.

A rejection learned after an approval ends one of two ways, depending on whether the approved
action has been carried out yet. They are shown in different places (section 8):

| | Path 1: still pending | Path 2: already carried out |
|---|---|---|
| Shown by | stack tests only (a local worker carries out an approval at once) | the UI walkthrough in section 8, and stack tests |
| Expected result | nothing is sent; the proposal is superseded | the action is kept; the case asks for reconciliation |

- **Path 1, pending:** receipt for 20 posted → follow-up approved and queued → rejection of 2
  recorded → dispatch attempted. Nothing is sent: `cancelled_before_dispatch`,
  `precondition_failed`.
  - The proposal is superseded, and the case is back for review on 18 accepted and 2 rejected.
  - The original receipt (version 1, 20 accepted) and the approval stay as recorded.
  - This holds when the rejection reaches OpsBridge through another case's read of the same
    PO: the worker and the casework MCP tool both refuse.
  - Tests: `test_a_rejection_learned_after_approval_stops_the_pending_follow_up`, `…_at_both_boundaries`.
- **Identical reimport:** the approval stands and the action is carried out once.
- **Path 2, already carried out:** the operation and the evidence it was approved on are kept,
  and nothing is resent or undone.
  - The re-investigation returns the case to review and records the audit event
    `operation.evidence_changed_after_execution`.
  - The Receiving tab says reconciliation is needed.
- **Outcome uncertain** (`outcome_unknown`): it stays on the existing review path. No new
  attempt is made, and nothing is claimed undone.

Freshness is local:
- A rejection still only in the purchasing system, not yet read by OpsBridge, does not stop a
  dispatch within the source age limit (`OPSBRIDGE_SOURCE_MAX_AGE_HOURS`).
- Any evidence change makes an approval stale, including a change that is not material to it:
  a false alarm at worst.

## 7. The case view

`GET /api/v1/tenants/{tenant}/cases/{case}/receiving` (permission `evidence.read`) and the
**Receiving** tab show, per PO line:

- **Quantities:** ordered, delivered, accepted, rejected, not delivered, returned, the
  replacement figures and the credit figures. An unknown quantity reads "unknown".
- **The split:** billed, matched and disputed, as the latest investigation concluded.
- **Each rejection:** its reason, receipt, occurrence (with its precision) and citations.
- **Unapplied events:** each one, with why it was not applied.

They also show:
- **The next question:** the owner, whether one is configured, the step, and the findings it
  rests on.
- **The hold policy's statement.**
- **What changed since each approval:** documents added or no longer current, and whether the
  approval needs review or the executed action needs reconciliation.
- **A timeline:** receipts, events, approvals and executions, by when OpsBridge recorded them,
  with when each occurred and "learned after approval" where it applies.

Positions come from the current records, through the same function the rules use.

## 8. Demo

The two 18-unit cases, side by side, with the local stack (`make doctor setup infra-up migrate
seed-demo` once):

    uv --directory backend run --frozen python -m opsbridge.cli receiving-pack --seed-simulator
    make dev

Sign in as `furn-investigator`. Open **New case** for `INV-8601` and for `INV-8602`, run each
with **Model: Rules only**, and open the **Receiving** tab:

| | A: INV-8601 (PO-4601) | B: INV-8602 (PO-4602) |
|---|---|---|
| Ordered / delivered / accepted / rejected | 20 / 18 / 18 / 0 | 20 / 20 / 18 / 2 |
| Not delivered | 2 EA | 0 |
| Rejection | none | 2 EA, "damaged in transit", GR-6602, 2026-10-20 |
| Billed / matched / disputed | 20 / 18 / 2 | 20 / 18 / 2 |
| Finding | `invoiced_exceeds_received` | `disposition_unrecorded`, plus "no owner for rejected goods" |
| Cited | order 20, invoice 20, receipt row 18 | order 20, invoice 20, receipt rows 18 and 2 with status and reason, the supplier's sentence |
| Next question | receiving: "Confirm delivered quantity for PO-4601" | manual review (ap-review): who asks the supplier how the 2 rejected chairs will be remedied |
| Payment | split shown; recommendation unresolved (furniture-ap@v2 sets no hold scope) | the same |

### The late rejection: two paths, two expected results

A rejection learned after an approval ends differently depending on whether the approved action
was already carried out (section 6). The UI walkthrough shows only path 2. Path 1 is shown only
by the stack tests.

**Path 2, in the UI: the action was already carried out. Expect reconciliation, not a block.**
A local worker carries out an approval within seconds, so by the time the rejection arrives the
action has run.
1. Open `INV-8609` (GR-6609: 20 accepted) and run it. It matches.
2. Create the proposal (a review note), and approve it as `furn-approver` in another browser
   profile. The worker carries it out.
3. Write the rejection the purchasing system records later:

        uv --directory backend run --frozen python -m opsbridge.cli receiving-pack --seed-simulator --late-events

4. Run the investigation again. Expect:
   - the case back for review, on 18 accepted and 2 rejected;
   - the review note that was carried out on the old evidence, still there, not resent or
     undone;
   - **Since approval:** "reconciliation: the action was carried out on evidence that has
     since changed; nothing was repeated or undone";
   - the timeline marking REJ-4609 (occurred 2026-10-20, date only) "learned after approval".

You will not see "nothing was sent" here. That result belongs to path 1.

**Path 1, in the stack tests: the action is still pending. Expect nothing sent.** The tests hold
the dispatch job so the rejection lands between approval and dispatch, which can't be staged by
hand:

    make infra-up migrate
    OPSBRIDGE_REQUIRE_INTEGRATION=1 uv --directory backend run --frozen pytest -m integration \
      tests/integration/test_receiving_disposition.py -k "stops_the_pending or both_boundaries"

Expected: 2 passed. They assert:
- the operation is `cancelled_before_dispatch` with `precondition_failed`, with no attempt and
  no remote task;
- the proposal is superseded;
- the worker's and the casework MCP tool's admission refuse it as stale, including when
  another case's read brought the rejection in.

Drop `-k …` to run all of this file's stack tests, path 2 included.

The same cases as an export, for `export-check` or a dry run (docs/pilot/quickstart.md):

    uv --directory backend run --frozen python -m opsbridge.cli receiving-pack --export-to ../var/receiving/export
    uv --directory backend run --frozen python -m opsbridge.cli export-check \
      --export ../var/receiving/export/demo-furniture --pack furniture

## 9. Tests

Scenarios 1–9 of the brief are covered by these tests. Each asserts stored facts, derived
quantities, citations, the question, the owner, and approval or execution behavior where it
applies:
- `backend/tests/domain/test_receiving.py`: rules and positions. It also holds every new
  summary to the number checks that model text must pass.
- `backend/tests/evidence/test_receiving_mapping.py`: import, including which packs a blank
  receipt status affects.
- `backend/tests/pilot/test_receiving_pack.py`: the 12 pack cases against labels written by
  hand before the rules ran.
- `backend/tests/pilot/test_export_check.py`: the export.
- `backend/tests/integration/test_receiving_disposition.py`: persistence, approvals,
  execution, the case view, tenant isolation, and routing under the synthetic policy version.
- `backend/tests/integration/test_migration_0025.py`: the migration.
- `frontend/src/pages/case/ReceivingPanel.test.tsx`: the tab.

## 10. Versions and earlier runs

- Workflow `investigation/1.5.0` lists receiving events and follows rules
  `opsbridge-rules/1.4.0`.
- Runs recorded under earlier versions replay with their own rules and collect no events.
- Every frozen recording reproduces case by case (`make eval-replay-check`).
- The canary baseline changed in two ways only: its rules version, and an added
  `hold_unconfigured` note on 18 quantity-mismatch cases. No outcome, type or next action
  changed.

One import behaviour changed: a receipt row with no status is `unstated`, no longer
`accepted` (section 3). Both shipped packs require a status, so their exports are unaffected.

Two pilot rehearsal artifacts changed:
- **The rehearsal's scenario A (INV-8401)** records 2 rejected chairs with no reason (its format
  has no column for one). It now asks receiving for the inspection record instead of proposing
  a delivery follow-up. Its label in `fixtures/pilot/rehearsal.yaml` was changed accordingly
  and says so.
- **The Records MCP tool** returns receipt lines as before, without reasons or events, because
  the frozen tool cassettes bind its output. The agent reads the rules' findings, not the
  events themselves.

## 11. Known gaps

- **Unread source changes:** a rejection recorded in the purchasing system but not yet read
  does not stop a dispatch within the age limit (local freshness, section 6).
- **Material changes:** any evidence change invalidates an approval, even an immaterial one.
- **Disposition owners:** no shipped policy names one, and the demo tenant's policy sends
  rejected goods to manual review. Configuring one is the organization's decision. The
  synthetic example version names one only to test routing (section 5).
- **Inspection evidence:** this is a recorded rejection reason. Inspection reports as
  documents are not modelled.
- **Coverage:** the IT hardware pack has no receiving-event mapping, and the agent tools don't
  see events.
- **Executed on changed evidence:** the flag is an audit event, and the tab reports it.
  Clearing it is the case's ordinary review; there is no separate reconciliation queue.
- **Accuracy:** nothing here is evidence of accuracy on any organization's receiving data.
