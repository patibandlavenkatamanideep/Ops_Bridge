# Pilot results

Three parts, kept apart:
- **A. Synthetic rehearsal:** filled in; synthetic data, no reviewer.
- **B. External first pass:** BLOCKED; no authorized export exists.
- **C. After adaptation:** empty until B is recorded.

Each is reported beside the others, never instead of them. Part B's pre-registration is
completed **before any external outcome is seen** and is not edited afterwards. The filled
copies of B and C hold case references and counts, and stay in the agreed environment until
the owner approves what leaves it.

## A. Synthetic rehearsal (recorded 17 November 2026)

What was run: the five synthetic cases in `fixtures/pilot/rehearsal.yaml`, written as an export
by `pilot-rehearsal-export`, checked by `export-check`, and investigated rules-only with
outbound writes disabled:
- `tests/integration/test_export_investigation.py::test_the_pilot_dry_run_reads_the_rehearsal_export_and_sends_nothing`
- `tests/integration/test_investigation_stack.py::test_pilot_scenario_a_delivered_is_not_accepted`
- `tests/integration/test_evidence_freshness.py::test_pilot_scenario_b_a_receipt_corrected_after_approval_is_not_acted_on`
- `eval-rules --corpus ../fixtures/pilot/rehearsal.yaml`: 5/5 against the hand-written labels

There is no reviewer: the labels in the reviewer column were written by hand from policy
`furniture-ap@v2` by the same author as the rules. This shows the preparation works. It is no
evidence of accuracy on anyone's data.

| Case | Importable or blocked | Evidence correctly cited | Proposed owner and action | Reviewer decision | Correction required | Reason |
|---|---|---|---|---|---|---|
| INV-8401 (scenario A) | importable | yes: order 20, invoice 20, receipt rows 18 accepted and 2 rejected with their status, the supplier's sentence; all valid | receiving, asked for the inspection record (`request_information`): the rejection records no reason | none; label agrees (relabelled for rules 1.4.0) | none | 20 arrived and 18 count; the 2 rejected chairs are billed. Before rules 1.3.0 the finding said "only 18 were received"; under 1.3.0 it proposed a delivery follow-up; from 1.4.0 the rejected units are a disposition question, and receiving is asked why they were rejected before the supplier is asked for a remedy |
| INV-8402 (scenario B) | importable | yes: order 10, invoice 10, receipt 8, then the corrected receipt's 10 | receiving, then a review note once the corrected receipt is read | none; label agrees for the first reading | none | The follow-up approved on 8 of 10 was not sent once the corrected receipt was read; the proposal was superseded and the case came back for review |
| INV-8403 | importable, with a note | yes: order and invoice lines; no receipt exists to cite | receiving, asked for the receiving record (`request_information`) | none; label agrees | none | Stops for missing evidence; never read as zero received |
| INV-8404 | importable | no discrepancy, so no finding to cite | none: review note | none; label agrees | none | Matched |
| INV-8405 | **blocked by the preflight** | not applicable | asked for the order (`request_information`) if opened anyway | none; label agrees | none | Its purchase order PO-4005 is not in the export; `export-check` lists it before any case is opened |

## B. External first pass: BLOCKED

Needs, before anything here is filled:
- the owner's written authorization naming the data;
- an authorized, sanitized export in `opsbridge-export/1` (docs/pilot/export-contract.md);
- one AP or procurement reviewer;
- a handful of blocked invoices chosen by the rule below.

None exists.

### Pre-registration (complete and date before the first case is opened)

| Item | Value |
|---|---|
| Recorded at, by | |
| OpsBridge commit | |
| Workflow and rules version | `investigation/1.5.0`, `opsbridge-rules/1.4.0` (shown on every investigation) |
| Pack and its SHA-256 | e.g. `furniture@v1`, `shasum -a 256 packs/furniture/pack.yaml` |
| Field mapping changes made for this export | none / list them (format only; a rule change is not a mapping change) |
| Policy and version | e.g. `furniture-ap@v2` |
| Model mode | rules only |
| `OPSBRIDGE_OUTBOUND_WRITES` | `disabled` |
| `OPSBRIDGE_SOURCE_MAX_AGE_HOURS` | the value, and why (the export's age plus the session) |
| Export: `source_system`, `extracted_at`, manifest SHA-256 | |
| `export-check` result | cases evaluable / blocked, with the report's SHA-256 |
| Case selection rule | e.g. "every invoice blocked between date X and date Y, in the order blocked, up to N" |
| Cohort file SHA-256 | `shasum -a 256 cohort.csv` |
| Evaluation questions | 1. Does OpsBridge's next action match the reviewer's independent one? 2. Is each figure the reviewer relied on cited correctly? 3. Which cases cannot be evaluated from the export, and why? 4. What corrections would the reviewer make to the drafted follow-ups? |
| Timing collected? | no / yes, with the start and finish in docs/pilot/reviewer-sheet.md |

### First-pass results (before any rule or mapping change)

| Case | Importable or blocked | Evidence correctly cited | Proposed owner and action | Reviewer decision | Correction required | Reason |
|---|---|---|---|---|---|---|
| | | | | | | |

Report counts beside the table:
- cases enrolled, importable and blocked;
- where the reviewer's next action and OpsBridge's agree;
- `needs_information` and `manual_review` outcomes, and whether the reviewer agreed;
- corrections, by kind.

If timing was collected, report how many cases were timed, with the median and the range. Do
not compare times with anything unless a comparable manual baseline was measured. With one
reviewer and a handful of cases these are examples and counts, not a customer accuracy claim.

## C. After adaptation

Only after B is recorded and only if the reviewer's corrections call for it. For each change
to a mapping or policy:
- name the exact change and who agreed to it;
- separate a format mapping from a business rule;
- give the new version or digest;
- then rerun the same cases separately.

| Change | Kind (mapping or policy) | Agreed by | New version or digest |
|---|---|---|---|
| | | | |

| Case | Result after the change | Different from B? | Why |
|---|---|---|---|
| | | | |

An adapted run shows what the change did on these cases. It is not evidence that the original
system generalized to this organization.
