# Production readiness

What stands between OpsBridge and a narrow procurement pilot, tracked against the external
production review of 27 October 2026 (findings A01–A18). Readiness is a property of a named
environment and workflow, not of the repository: it needs one verified source integration,
deployed controls and measured operator results, none of which exist yet. Fixing every
locally reachable finding does not make the system production ready.

Evidence levels used below: **local** (real PostgreSQL, Keycloak, simulator and browser on the
development machine), **CI** (hosted GitHub Actions), **sandbox**, **staging**, **pilot**.
Nothing here has reached sandbox, staging or pilot.

## Baseline

| Item | Value |
|---|---|
| Reviewed `main` | `468dae7` (25 October 2026) |
| Reviewed PR | #17, head `732e84d`, open; 78 files; hosted CI run 40528472529 green |
| Work branch | `feat/opsbridge-foundation` (PR #17's branch); new commits follow `732e84d` |
| Baseline gates | `make verify` on the tree of `732e84d`: 15/15 stages (unit 381, integration 222, browser 31); `make verify-strict` fails only on the skipped OCR tests (Tesseract not installed locally; they run in CI and the image) |
| Environment | macOS host; PostgreSQL 16, Keycloak, SeaweedFS in Podman; no cloud account; no real model or customer system contacted |
| Review documents | supplied by the owner in the session; not committed (the repository is public) |

## Finding matrix

| ID | Finding | Status | Package | Evidence | Depends on |
|---|---|---|---|---|---|
| A01 | Malformed or empty invoice lines became a clean match, and the case could be resolved | Fixed, verified locally | 1 | See "Package 1" below | — |
| A02 | Duplicate PO line identifiers silently overwrote one another | Fixed, verified locally | 1 | See "Package 1" below | — |
| A03 | "Instability" was correctness instability: two different wrong actions counted as stable | Fixed, verified locally (scoring 2; v1 kept under scoring 1) | 4 | See "Package 4, metrics" below | — |
| A04 | Response size checked after buffering the whole body | Open, confirmed on this branch (`connectors/purchasing.py::_get`) | 3 | — | — |
| A05 | Casework MCP admission weaker than the executor's; rebuilds outgoing text from parameters | Open, confirmed statically on this branch (`mcp_services/casework.py`) | 2 | — | — |
| A06 | Verifiers load the private signing key; credential references not tenant-owned | Open, confirmed on this branch (`factory.verifier`, `services/connections.credential`) | 3 | — | — |
| A07 | Important fixes only in open PR #17 | This branch contains PR #17; the merge is the owner's | 0 | PR #17 green in CI at `732e84d` | Owner merges #17 |
| A08 | No actual purchasing source | Blocked | 5 | Contract harness exists (`purchasing-source/1`, 21 contract tests) | Owner chooses a system or export and authorizes access |
| A09 | AI usefulness not measured on its actual task | Blocked for real-model evidence | 6 | Mock-only results; no paid calls made | Model credential, budget and authorization |
| A10 | Labels by the same author; held-out set public | Blocked | 6, 9 | — | Independent labellers and new data |
| A11 | Staging is a plan only | Blocked | 8 | `docs/deployment.md` plan | Cloud account, choice and spending authorization |
| A12 | `main` unprotected; no release promotion | Recommendation pending; repository settings are the owner's | 8 | — | Owner applies branch rules |
| A13 | Replay compares next actions only; mock replay depends on rules-only trials running first | Open | 4 | Recorded in `docs/review-findings.md` | — |
| A14 | Operator workflow unvalidated | Partly: PR #17 ownership, views, settings sections, keyboard focus | 7, 9 | Browser tests | Operator sessions |
| A15 | Capacity and spend bounds unmeasured | Open | 3, 8 | — | Staging for real load figures |
| A16 | Identity and privacy lifecycle incomplete | Open | 3, 8 | — | Identity-provider deprovisioning policy from the owner |
| A17 | Image lacks the evaluation evidence the repository verifies | Open, confirmed (`.dockerignore` excludes `evals/runs`) | 8 | — | — |
| A18 | Large modules; status documents drift | Ongoing with each package | all | — | — |

Earlier findings keep their own history in `docs/review-findings.md`: F01–F18 (review of
22 October) and X1–X7 (found along the way). A01–A18 do not replace them; where they overlap
the earlier entry is referenced.

## Package 1: canonical business-input integrity (A01, A02)

Every regression below failed on `732e84d` for the stated reason before the fix.

**One validation boundary** (`domain/validation.py`). Each canonical record must have its
identifiers present and storable, statuses and kinds from the supported set, quantities,
prices and discounts as finite Decimals within the input contract (18 integer and 12 decimal
digits), discounts neither negative nor above the line, at least one line, and no two lines
whose identities normalize to the same key. The pack mapping builds no record that fails
(the document is marked as needing review), and `reconcile` refuses to conclude from input
that fails, however the records were constructed.

**Matcher** (rules `opsbridge-rules/1.2.0`):
- Input that fails validation gives one review finding per kind of problem
  (`record_lines_missing`, `line_identity_duplicate`, `record_identity_duplicate`,
  `status_unsupported`, `value_unsupported`, `discount_unsupported`, `identifier_invalid`).
- An invoice of charges only is reviewed (`goods_lines_absent`): charges are excluded from
  matching, so nothing on it would be checked.
- A receipt that appears twice is refused instead of counted twice; several lines for one
  PO line within a receipt stay allowed.
- Arithmetic runs in an 80-digit context. Python's default 28 digits rounded two different
  30-digit totals to the same value and hid an excess.
- Tests: `tests/domain/test_input_integrity.py` (24 failed before; 2 guards passed before
  and after).

**Mapping** (`mapping.py`): line collections that are an object, null or text are a problem,
not "no lines"; a record with any unreadable line is not built; a record whose identity
repeats within one document builds neither copy. Tests: `tests/evidence/test_mapping_integrity.py`
(8 failed before).

**Investigation** (`workflow/investigation.py`): while a decisive document of the case
(invoice, PO, receipt, allocation history) has a newest version that could not be read
completely, nothing is concluded (`source_document_needs_review`, or the existing
`invoice_extraction_needs_review`). Before, a receipt whose return line could not be read
still let the goods count as received.

**Database** (migration 0019): unique line identity per PO and per invoice, compared as the
matcher compares it (trimmed, upper-cased). Existing duplicates are listed and the upgrade
stops without changing anything. `tests/integration/test_migration_0019.py` rehearses the
refusal under forced RLS, the upgrade once the duplicate is resolved, a refused new
duplicate (case and space variants), and the downgrade.

**End to end** (`tests/integration/test_input_integrity.py`, PostgreSQL, simulator, API):
INV-7001 with lines as an object, null, text, an empty list, or with one unreadable line
matched on the old code (6 of 6 assertions saw `matched`); now each is `manual_review` and
`POST /resolve` is refused with `resolution_evidence_missing`. A second receipt with an
unreadable return line gave `matched`; now `manual_review`. Its readable control gives
`discrepancies_found` (10 invoiced, 8 net received).

**Canary**: 64 of 64 cases unchanged; only the rules version moved (baseline updated in its
own commit).

**Benchmark replay**: the first full gate failed closed on `fur-security-303` (an image-only
invoice): the new document check reached it first and wrote its own summary, and the
summary is part of the recorded model request. Outcome and reason code were already the same.
A collected invoice now keeps the earlier wording exactly (an uploaded one names its own
problem), and every recorded run reproduces case by case again: held-out 36/40 in both
modes, security 7/7. No cassette was re-recorded.

**Not covered**: allocation history carries no unit and is taken in the PO line's unit;
excluded tax and shipping lines remain "charges" disclosed by a note; precision limits are
those of the input contract, not of any real source (none is integrated).

## Package 4, metrics: versioned scoring (A03)

**Reproduced**: the review's probe (expected a task; repeats gave escalate, ask, escalate)
reports a flip rate of 0 and no case with any flip under scoring 1: it measured whether the
next action was *correct*, not the action. Scoring 1 also took a case's outcome and types
from the first repeat with the majority action.

**Fix**: scoring schemes are versioned (`evaluation/manifest.py`). Scoring 1 stays byte for
byte as frozen in `opsbridge-bench-v1` and still grades it, so every v1 receipt, result and
replay is unchanged. Scoring 2, frozen into every new benchmark, reports per case:
- action disagreement: 1 − most common action / repeats, an error being a verdict of its own;
- correctness disagreement;
- outcome-and-types disagreement;
- the share of errored repeats.

Each majority is taken on its own axis, with ties reported. A case counts correct as a whole
only if its most common whole verdict is untied and matches. A case with missing, duplicated
or relabelled repeats is incomplete and listed, and the acceptance gate reports
`incomplete` instead of pass or fail. `verify` accepts a plan whose scoring is a scheme the
code implements; the API and the evaluation page show each run's scheme and, for scoring 1,
what its instability figure measured.

**Evidence**: `tests/evaluation/test_metrics_scoring2.py` (7, all failed before): on the
probe, action disagreement 1/3 and correctness disagreement 0, with scoring 1's 0 kept; the
same action with different findings is visible; errors are counted; per-axis majorities;
incompleteness; the frozen v1 plan still verifies; `eval-rescore` reads a recorded run
without changing it. The existing comparability tests keep their assertions (only the way
they simulate a changed grader moved to the scheme registry).

**Derived reading of the recorded v1 runs under scoring 2** (`opsbridge eval-rescore`, prints
only): no disagreement on any axis in any recorded split or mode (held-out 0/40, validation
0/20, security 0/7, both modes), all complete. The metric was wrong, but it hid nothing in
these results, because the rules and the mock model are deterministic.

**Not done**: no `opsbridge-bench-v2` is frozen yet. A new benchmark should come with the
independently labelled corpus of A10; freezing v2 on the public v1 cases would invite
out-of-sample claims they cannot support.

## Work plan

1. Done: Package 1 (A01, A02) and Package 4's metric fix (A03).
2. Package 2: one admission contract for the executor and the Casework MCP tool (A05),
   sealed outbound text and destination, a separate recovery authority.
3. Package 3: streaming response bounds (A04); public-only verifiers and tenant-owned
   credential references (A06).
4. Package 4: versioned behavioural replay and trial isolation (A13).
5. Package 8, in-repository part: evidence packaging decision and test (A17); a release
   workflow and branch-rule settings prepared for the owner (A12).
6. Blocked until the owner provides access or decisions: A08 (source), A09/A10 (model
   budget, independent labels), A11 (cloud), A14 (operators), A16 (deprovisioning policy).
