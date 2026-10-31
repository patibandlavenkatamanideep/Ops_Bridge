# Pilot plan (proposed; not started)

No pilot has been run, no participants recruited and no operator measurements exist. This
plan is what a pilot owner would agree to before one starts; thresholds below are proposals
for the pilot owner to change and sign off **before** any session.

## Goal and question

Does OpsBridge help AP or procurement investigators reach a correct, evidenced next action
for blocked invoices, compared with how they work today, without unsafe actions? The pilot
measures people doing their real task; it does not re-run the synthetic benchmark.

## Scope

- One organization, one purchasing context (pick the one with the most blocked invoices of
  the three types: missing receipt, quantity mismatch, price mismatch).
- 4–6 investigators and 2–3 approvers who already handle these exceptions.
- 2–4 weeks; follow-up tasks go to a sandbox or a dedicated project in the task system, not
  to production queues, unless the pilot owner authorizes otherwise in writing.

## Prerequisites (each must be done before the first session)

1. Written authorization from the pilot owner, including what data may be used.
2. Read-only access to the purchasing/ERP exports or API for the pilot scope, through a
   service account with least privilege; no write access to purchasing or payments.
3. Pack mapping and policy for the organization's formats and tolerances, reviewed by an
   AP lead (docs/integration-onboarding.md); validation cases from their own past exceptions,
   labelled by them, run before go-live.
4. Identity: the organization's OIDC provider, with pilot users and roles.
5. Model use decided: rules only, a local model, or a hosted model with opt-in, a spend
   ceiling and a data-processing agreement; redaction is pattern-based, not anonymization.
6. Hosting agreed (docs/deployment.md), encrypted storage, backups, retention periods,
   restore drill done on synthetic data.

## Consent and data boundaries

Participants consent to session observation and timing; they can stop at any time. Only
cases within the authorized scope are used. No raw documents leave the agreed environment;
measurement notes record case ids, times and outcomes, not document content. Results are
reported in aggregate.

## Tasks

Each participant works a mix of real blocked invoices (target 20 per participant across the
three types), half with their current process and half with OpsBridge, order randomized per
participant, comparable cases paired by type where possible. Approvers review proposals as
they normally would.

## Measures (defined before the pilot)

| Measure | Definition |
|---|---|
| Correct next action | Matches an AP lead's independent decision, recorded before seeing OpsBridge output |
| Time to a correct investigation | Start of work to a decision the lead agrees with, per case |
| Time to approved follow-up | Decision to approved follow-up (OpsBridge arm) |
| Escalations | Manual-review / needs-information outcomes and whether the lead agreed |
| Unsafe outcomes | Wrong action approved; duplicate or unintended external effect; access outside scope — target zero |
| Extraction corrections | Fields corrected by investigators, by document type |
| Usability | Short survey after each session; open feedback |

Report per measure with counts, denominators and ranges; paired by participant. With this
size, differences are indicative, not statistically conclusive.

## Launch criteria for wider use (proposal, to be agreed)

- Zero unsafe outcomes in the pilot.
- Correct next action at least as often as the current process, by the AP lead's judgement.
- Median time to a correct investigation not worse than the current process.
- Every escalation explainable from the case evidence.
- Operators can complete the operator guide tasks without help after one session.

## Feedback and stopping

Weekly review with the pilot owner: defects, mapping gaps, policy questions. Stop or pause
on any unsafe outcome, data handling concern, or participant request.

## First step: a single-source pilot (protocol `opsbridge-pilot/1`)

Before the study above, the production review (closure review of 30 October 2026, step 5)
asks for one pilot on one real purchasing source:
- an authorized, sanitized export;
- one AP or procurement reviewer;
- four measures compared with the existing manual process: extraction completeness,
  investigation accuracy, approval corrections and time to resolution.

The measures below were defined, and the report that computes them was written and tested on
synthetic data, before any pilot data existed. They are not changed once a pilot starts. A
change after that is a new protocol version, and it is reported beside the original, never
instead of it.

**Prerequisites.** Items 1–6 of the prerequisites above, for one export and one pilot
tenant. The export is held to `purchasing-source/1` (`OPSBRIDGE_PURCHASING_EXPORT_DIR`,
production-readiness Package 5). Its pack mapping is reviewed before the first case.
`export-check` (below) reads every record of the export through that pack, the way an
investigation does, and stores nothing. The first case is opened only when each problem it
lists has been fixed in the export or the mapping, or accepted by the owner and the reviewer
as a fact of the source (for example, an invoice whose purchase order the system really
lacks).

**Procedure.**
1. The owner and the reviewer fix the sample before any case is opened: the blocked invoices
   in an agreed period, up to an agreed number. Each invoice is assigned to an arm in
   alternation, in the order it was blocked: `opsbridge` (worked in OpsBridge) or `manual`
   (worked with the existing process).
2. For each sampled invoice, the reviewer records an independent decision in the reference
   file **before its case is opened in OpsBridge**. A decision is blind only if it was
   recorded before intake. A later decision is reported separately and is not part of the
   headline accuracy, because the reviewer may have seen OpsBridge's output.
3. `opsbridge` cases are opened in the pilot tenant and worked and approved as usual.
4. `manual` invoices are worked with the existing process. Their start, decision and
   resolution times come from that process's own records where they exist. What the process
   decided is recorded in the reviewer's vocabulary.
5. The report is computed at the agreed cut-off. Neither the reference file nor the report
   contains document content, and both stay in the agreed environment.

**Reference file** (CSV, UTF-8, one row per sampled invoice; `opsbridge/pilot/reference.py`):

| Column | Meaning |
|---|---|
| `arm` | `opsbridge` or `manual` |
| `case` | The OpsBridge case number, or for `manual` a pseudonymous reference (never an invoice number or a supplier's name) |
| `reviewed_at` | When the reviewer recorded the decision (ISO 8601 with a time zone) |
| `outcome` | `matched`, `discrepancies_found`, `needs_information` or `manual_review` |
| `types` | Discrepancy types, separated by `;`: `missing_receipt`, `quantity_mismatch`, `price_mismatch` |
| `next_action` | `decision:action_type`, as OpsBridge labels it: `no_discrepancy:record_review_note`, `propose_action:create_follow_up_task` (internal follow-up), `propose_action:create_supplier_message_draft` (supplier query), `request_information:create_follow_up_task` (missing information), `escalate:assign_case` (a person reviews) |
| `process_outcome`, `process_types`, `process_next_action` | `manual` only, optional: what the existing process decided |
| `started_at`, `decided_at`, `resolved_at` | `manual` only: work began (required), the follow-up was decided, the invoice was resolved; blank if not reached |
| `excluded` | A reason to leave the invoice out of every measure (for example, outside the agreed scope) |

The file is read strictly. A misspelled value, a time without a time zone, a repeated case,
or an OpsBridge timing entered by hand is refused, with its line, and nothing is measured.
OpsBridge's decisions and times come only from its records.

**Measures.** The unit is the case. Excluded invoices are in no measure. An OpsBridge case
without a reference row is in every measure except accuracy. Failures stay in their
denominators.

| Measure | OpsBridge arm | Manual arm |
|---|---|---|
| Extraction completeness | Share of cases whose first investigation had complete evidence (every record found, every document parsed; `evidence_readiness = complete`), Wilson 95%. Also: parse status of each document read, status of each extracted field, field corrections and the cases with any | Not measured: the process extracts nothing to compare |
| Investigation accuracy | The first investigation's verdict compared with the reviewer's decision on next action, outcome, types and the whole case, Wilson 95%. Blind and not-blind decisions are reported separately; the headline is blind next-action agreement. A referenced case that was never investigated counts as wrong. Each disagreement is listed by case number for adjudication | The process's decision compared with the reviewer's, on the same axes; invoices with no recorded process decision are counted |
| Approval corrections | Each proposal and its edits: approved as drafted, approved with changes (parameters differ from the investigation's draft; changed fields counted), rejected, or undecided. Correction rate = (changed + rejected) / decided, Wilson 95% | Not measured: the process has no draft to correct |
| Time to decision and to resolution | Hours from intake to the first approved follow-up or the resolution, whichever is first, and from intake to resolution | Hours from `started_at` to `decided_at` and to `resolved_at` |

Times are reported as n, reached, still open at the cut-off, median and p90. Open cases are
counted, never dropped. The arms start their clocks at different points: OpsBridge at intake,
the manual arm when work began. Any waiting before intake is therefore not in the OpsBridge
figure, and the report must say so wherever the arms are compared.

**Running it.** Before the first case, check the export against its pack:

    uv --directory backend run python -m opsbridge.cli export-check \
      --export <export dir>/<organization> --pack <pack id> --out <check.json>

For each listed record it checks that:
- the file is present, within the size limit, and matches its digest;
- the pack reads its collection;
- it parses and maps without problems;
- an invoice's or purchase order's number is its key, since a case fetches it by that number;
- a record read by purchase order is listed under the order it is for;
- each invoice's purchase order is in the export.

It exits 1 if anything needs review, and 2 if the export or the pack can't be opened
(`opsbridge/pilot/export_check.py`). The report holds keys, counts and problems, not document
content. A mapping problem can quote the value that failed, so the report stays with the
export.

At the cut-off, compute the measures:

    uv --directory backend run python -m opsbridge.cli pilot-report --tenant-id <pilot tenant> \
      --since <window start> --until <window end> --as-of <cut-off> \
      --reference <reference.csv> --out <report.json>

It reads one tenant's cases opened in the window, under that tenant's row-level security, in
a read-only transaction (`opsbridge/pilot/collect.py`). The measures are computed in
`opsbridge/pilot/measures.py`. Each investigation is read as a verdict by the same function
the benchmark scores with (`opsbridge/evaluation/verdict.py`).

**Limits.**
- The reference is one person's judgement: there is no second reviewer and no adjudication
  beyond the disagreement list.
- With one reviewer and a few dozen invoices, differences between the arms are indicative,
  not conclusive.
- Blindness is shown only by the recorded time. A decision recorded before intake could not
  have seen OpsBridge's output; nothing else is enforced.
- Manual timings are only as good as the records they come from.

**Status:** prepared and tested on synthetic data (`tests/pilot/`, 27 tests, and
`tests/integration/test_pilot_report.py`, 2 tests). No pilot has started: no export,
reviewer or authorization exists yet.
