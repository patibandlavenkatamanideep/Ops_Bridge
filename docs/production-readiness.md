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
| A04 | Response size checked after buffering the whole body | Fixed, verified locally (purchasing source, task trackers, Ollama) | 3 | See "Package 3, transport bounds" below | — |
| A05 | Casework MCP admission weaker than the executor's; rebuilds outgoing text from parameters | Fixed, verified locally (real MCP, PostgreSQL, Keycloak) | 2 | See "Package 2" below | — |
| A06 | Verifiers load the private signing key; credential references not tenant-owned | Partly fixed, verified locally: public-only verifiers with key rotation, tenant-registered credentials. Not done: per-role database and secret access, parser sandbox (staging work) | 3 | See "Package 3, keys and credentials" below | Staging (A11) for per-role secrets |
| A07 | Important fixes only in open PR #17 | This branch contains PR #17; the merge is the owner's | 0 | PR #17 green in CI at `732e84d` | Owner merges #17 |
| A08 | No actual purchasing source | Blocked | 5 | Contract harness exists (`purchasing-source/1`, 21 contract tests) | Owner chooses a system or export and authorizes access |
| A09 | AI usefulness not measured on its actual task | Blocked for real-model evidence | 6 | Mock-only results; no paid calls made | Model credential, budget and authorization |
| A10 | Labels by the same author; held-out set public | Blocked | 6, 9 | — | Independent labellers and new data |
| A11 | Staging is a plan only | Blocked | 8 | `docs/deployment.md` plan | Cloud account, choice and spending authorization |
| A12 | `main` unprotected; no release promotion | Prepared, not applied: ruleset file and release workflow (build from locks, SBOM, provenance attestation, verification, promote by digest) | 8 | See "Package 8, governance and releases" below | Owner applies the ruleset and pushes the first tag |
| A13 | Replay compares next actions only; mock replay depends on rules-only trials running first | Fixed in part, verified locally: behavioural replay comparison; run order explained, declared and tested to fail closed. Its cause (a finding-text defect) is fixed with the next frozen benchmark | 4 | See "Package 4, behavioural replay" below | — |
| A14 | Operator workflow unvalidated | Partly: PR #17 ownership, views, settings sections, keyboard focus | 7, 9 | Browser tests | Operator sessions |
| A15 | Capacity and spend bounds unmeasured | Partly: one time budget for reading the source per investigation. Load figures, SLOs and spend accounting need staging | 3, 8 | See "Package 3, collection budget" below | Staging and the owner's expected load |
| A16 | Identity and privacy lifecycle incomplete | Open | 3, 8 | — | Identity-provider deprovisioning policy from the owner |
| A17 | Image lacks the evaluation evidence the repository verifies | Decided and tested locally: summary-only in the image, full verification from the repository; image check in CI | 8 | See "Package 8, evidence packaging" below | — |
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

## Package 2: one admission contract for the executor and Casework MCP (A05)

**Reproduced on this branch** (`tests/integration/test_casework_admission.py`, real MCP client
and server, PostgreSQL, Keycloak service tokens, simulator; each failed before):
- a create that reached the casework tool after the approval expired, after a cancellation
  request, or after the evidence was corrected still landed (3 cases);
- a replayed request with a valid delegation, while the operation was executing and the
  tracker had no idempotency keys, created two more tasks (3 instead of 1);
- with a different redactor in the casework service, the task carried text other than the
  approved text (the title came out upper-cased);
- once the approver was revoked, reconciling a task already sent was refused.

**Fix**:
- `services/execution_admission.py` holds the executor's admission (cancellation, proposal
  and approval state, expiry, the approver's current authority, evidence and policy
  freshness, destination and text), moved there unchanged, and `approved_request`, the one
  builder of the request that is sent.
- The casework tool admits a create with that function when the request reaches it. The
  executor's attempt id travels in the call and in the signed delegation, and the tool
  admits each attempt once (`attempts.admitted_at`, migration 0020; the MCP role may update
  that column only).
- The tool sends the sealed outbound text and builds a connector only for the approved
  destination (simulator tasks in the tenant's own organization). Its deployment-wide GitHub
  fallback is gone: GitHub issues are sent by the worker with the tenant's connection.
- Read-back and reconciliation of an admitted create no longer need the approver's current
  authority.
- A refusal comes back as `NotAdmitted`; the executor runs its own admission again and
  finishes the operation `cancelled_before_dispatch` with the same reasons.

**Human review of an unknown outcome** now uses the executor's completion
(`finish_operation`): a follow-up a person found moves the case to `follow_up_pending` and
drains its outbox event (before, the case stayed awaiting review and the event stayed
undelivered: `tests/integration/test_unknown_outcome_review.py`). The confirmation is
labelled `human_review`, and the operation is counted once in the metrics. The API role may
set `outbox_events.delivered_at` (0020).

**Parity**: `test_direct_and_mcp_refuse_the_same_stale_approval` (expired, corrected ×
direct, MCP) ends both transports in `cancelled_before_dispatch / precondition_failed` with
nothing sent. It passed before too, because the executor checked before calling; the delayed
cases above are what only the tool can catch.

**Migration 0020** was downgraded and upgraded on the development, test and evaluation
databases, and the column and grants were checked in each with `psql`.

**Correction to Package 1**: the commit for migration 0019 said it had been applied to the
evaluation database. It had not: a shell loop passed `-x db=…` as one word, so Alembic used
the development database each time. The evaluation database was upgraded from 0018 on
2026-10-27 together with 0020 (its duplicate scan found nothing).

**Correction to A03**: the browser stage of A03's gate ran against a dev stack started
before the A03 API change (the stack does not reload code). The A03 API is covered by
`test_evaluation_api_shows_own_pack_cases_and_validates_run_ids`, which runs current code; the
dev stack was restarted for Package 2's gate.

**Not covered**: the admission point is when the tool reads committed state; a change
committed after that is too late for the write in flight, and a request the tracker accepted
cannot be withdrawn. GitHub through the casework tool is not supported.

## Package 3, transport bounds (A04)

**Reproduced on this branch**: the purchasing connector read all 7,340,032 bytes of a 7 MiB
streamed record before refusing it against its 5 MiB limit; the simulator task connector had
no limit at all, and a 201 reply to a create whose body was malformed, lacked the task id or
was 2 MiB of junk escaped as a raw `JSONDecodeError` or `KeyError` instead of an ambiguous
outcome.

**Fix** (`connectors/http_transport.py`): every outside response OpsBridge parses is
streamed and its *decoded* bytes counted as they arrive; the read stops and the connection
closes as soon as the count passes the limit, whatever Content-Length claims (a declared
length past the limit is refused before reading). Gzip and deflate are decoded here with each
step capped at what the limit still allows; other encodings are refused, and only these two
are offered. A total deadline covers the whole read, so a body trickling in just inside the
per-read timeout stops there. The errors subclass httpx's own, so callers keep their
meaning:
- purchasing source: oversized or undecodable → invalid record; total deadline →
  unavailable (never "not found"); limit 5 MiB, deadline 3× the request timeout;
- task trackers (simulator, GitHub): an unreadable or oversized reply to a create is
  **ambiguous** (the task may exist) and is reconciled; an unreadable read-back or lookup is
  "lookup unavailable", never proof of absence; limits 1 MiB and 8 MiB;
- Ollama: model list 1 MiB, reply 8 MiB; an unreadable reply is "model unavailable", so the
  rules-only fallback applies.
The executor now closes each dispatch's connector, and the GitHub connector has a `close`.

**Evidence**: `tests/test_http_transport.py` (13): 7 MiB stream stopped within one chunk of
5 MiB; a lying Content-Length; a declared oversize refused before reading; 64 MiB of zeros
compressed with gzip and deflate stopped at the limit; an ordinary compressed body read back
exactly; Brotli refused; a slow drip stopped at the total deadline; the purchasing source
stopping early and reporting a stalled read as unavailable; three unreadable 201 replies to a
create classified as ambiguous. The connector cases failed before as described. The 36
source-contract tests pass unchanged.

**Not covered**: the MCP SDK's HTTP client and the Anthropic SDK read their own responses
(not routed through this module); a response already held in memory (test transports only)
is checked after the fact. Total source-collection time is bounded per request (pages ×
deadline), not as one budget.

**Open observation**: `tests/contracts/test_github_connector.py::test_ambiguous_failures_never_claim_absence[timeout_after]`
(a create whose reply comes after the client's timeout must be ambiguous) failed 3 times in
73 whole-file runs with the bounded reader: once in a gate run during which the Mac slept,
twice while the VM clock was being repaired. It never failed in 42 runs without it. Run
directly, a timed-out create returned a task 1 time in 250 with the reader and 0 in 170
without (fresh and long-running fake servers). The difference is not significant and no
mechanism was found; it is recorded rather than dismissed. Watch for it in CI.

## Package 3, keys and credentials (A06)

**Reproduced on this branch**:
- building a delegation verifier in a deployed environment demanded the private signing key
  (`factory.verifier` called the private-key loader and took its public half), so every MCP
  service that only verifies held a key that can mint delegations;
- a tenant administrator could set a task connection naming another tenant's credential
  reference, an unregistered one, or one meant for another repository, and dispatch then read
  that secret (all accepted before: `tests/integration/test_credential_registry.py`).

**Fix**:
- Delegations carry a key id (`kid`); verifiers are built from public keys only
  (`OPSBRIDGE_DELEGATION_PUBLIC_KEYS` or `…_PUBLIC_KEY_PATH`), accept several while keys
  rotate, refuse an unknown key, and accept a delegation without a key id only when a single
  key is configured. A deployed verifier without public keys refuses to start; only
  development derives one from its local key file. `init-keys` writes the public key file and
  the compose MCP services read it.
- The deployment registers each credential reference with the tenant, connector and
  repositories it may serve (`OPSBRIDGE_CONNECTION_CREDENTIALS`). A connection naming an
  unregistered or foreign reference is refused (`credential_not_registered`), and dispatch
  checks the registration before reading the secret, so a connection row written around the
  service still cannot use another tenant's secret. Existing connections whose reference is
  not registered stop sending until the deployment registers it (fail closed).

**Evidence**: `tests/mcp/test_verifier_keys.py` (5; 4 failed before, the development
derivation passed before and after) and `test_credential_registry.py` (5; 4 failed before).
The existing connection tests needed the registration in their settings; their assertions
are unchanged.

**Not done** (staging, A11): per-role database credentials and secret mounts (the compose
development volume still holds both delegation keys; the containers share one environment
block), and a parser sandbox with no secrets or network beyond the Linux memory cap.

## Package 8, evidence packaging (A17)

**Decision**: the runtime image serves evaluation *summaries*: results, frozen manifests,
the signed ledger and its public key. It does not carry the evidence bundle (`evals/runs`:
cassettes and bound trial files; 83 files, 2.1 MB today), and it does not claim to reproduce
runs. Full artifact verification and replay run
from the repository at the commit each result records.

**Before**: `eval-verify` in such an installation reported every record as FAILED (its bound
files are absent), which reads like tampering. **Now** it verifies what is present, the
ledger chain and signatures, and says the artifacts are not in this installation and where to
verify them, exiting 3 (partial) rather than passing. `--summary-only` asks for exactly the
ledger check and exits 0 when it holds; a tampered ledger still fails.

**Evidence**: `tests/evaluation/test_evidence_packaging.py` (3), using an installation laid
out like the image (evals/ without runs/): partial and explicit by default (failed before:
exit 1 with FAILED records), summary-only passes, a tampered ledger fails. In the repository
`eval-verify` still checks all 26 records' artifacts (gate stage unchanged). The CI image job
runs both commands inside the built image; the first hosted run of that check is pending.

**Not done**: a separately versioned, checksummed evidence bundle (for example a release
asset) that a deployment could fetch and verify. Today the bundle lives in the repository.

## Package 4, behavioural replay (A13)

**Before**: a replay reproduced its recording if every repeat's next action matched; outcome,
types, reason codes, citations, model calls and tool calls could change unnoticed.

**Now** (`opsbridge-replay/2`, `evaluation/reproduce.py::trial_differences`): each trial is
compared field by field with its recording, ignoring only generated ids, timings and replay
provenance; a missing or extra repeat is a difference; a field the recording lacks is listed
as not compared. New trials also record digests of the findings (values and evidence
locators) and citations. `eval-replay-check` and `eval-bench --replay` use it. Every recorded
v1 run reproduces under it (held-out 36/40 both modes, security 7/7); the finding and citation
digests are reported as not in those recordings. A first version flagged 120 differences that
were only the explanation's provider label (`model:mock` recorded, `model:replay` replayed),
which is provenance and is now compared as model versus template.

**Run order**: replaying the frozen security recording's mock trials alone diverges at
`fur-security-304` and fails closed; in reversed order all seven cases fail. Cause: read
problems are listed only on a document's first ingestion in a run, so the invoice-extraction
finding, and the model request built from it, depend on which trial investigated first. This
is also a product defect (a re-investigation loses the reason an invoice could not be read),
listed in docs/STATUS.md. Fixing it changes the recorded requests, so it is planned with the
next frozen benchmark; the frozen order is declared, and
`tests/integration/test_replay_mode_order.py` checks that the frozen order reproduces and
other orders fail closed rather than differ quietly. Three validation cases recorded fresh
replay in any order (no such document among them).

**Evidence**: `tests/evaluation/test_reproduce.py` (3 new), `test_replay_mode_order.py` (3).

## Package 8, governance and releases (A12)

**Prepared** (nothing applied; repository administration and publishing are the owner's):
- `infra/github/main-ruleset.json`: pull requests only, the four CI jobs required on the
  latest commit, review threads resolved, no force push or deletion; administrators may
  bypass only through a pull request (break-glass). Required approvals 0 while there is one
  maintainer. The check names were matched against the CI job names. The apply command is in
  docs/deployment.md.
- `.github/workflows/release.yml`: on a version tag it refuses a commit whose required CI
  checks did not all succeed, builds from the locks, pushes to GHCR, attaches an SPDX SBOM and
  a build-provenance attestation, verifies the attestation, and reports the digest to
  promote. Actions pinned to commits of releases at least two weeks old.

**Evidence**: YAML and JSON parse; the required-check names match CI. The workflow has not
run (no tag), so its first use is its first test, and no attestation exists yet.

**Not done**: a staging deployment that promotes the attested digest, rollback rehearsal, and
a vulnerability policy beyond "fixable critical" (the CI image job's current gate).

## Package 3, collection budget (A15, in part)

**Before**: each source request had a deadline but an investigation's reading as a whole had
none: a receipt listing may follow up to 50 pages and fetch every record, so a slow source
held an investigation for minutes. In the regression, records delayed 0.7 s each against a
1 s budget were all read (2.6 s) and the investigation concluded `discrepancies_found`.

**Now**: `source_collection_seconds` (default 120) bounds the whole collection; each read
gets what is left of it. When it runs out, collection stops, keeps what it read, and the
investigation goes to manual review (`source_collection_timed_out`) instead of concluding from
part of the data. Within the budget nothing changes.
`tests/integration/test_collection_budget.py` (2; the timed-out case failed before).

**Not done**: load measurement on a staging environment (p95/p99, queue age, pool use),
SLOs agreed with an operator, and spend accounting against provider billing; the 120 s
default is provisional.

## Work plan

1. Done: Package 1 (A01, A02), Package 4's metric fix (A03), Package 2 (A05).
3. Package 3: streaming response bounds (A04); public-only verifiers and tenant-owned
   credential references (A06).
4. Package 4: versioned behavioural replay and trial isolation (A13).
5. Package 8, in-repository part: evidence packaging decision and test (A17); a release
   workflow and branch-rule settings prepared for the owner (A12).
6. Blocked until the owner provides access or decisions: A08 (source), A09/A10 (model
   budget, independent labels), A11 (cloud), A14 (operators), A16 (deprovisioning policy).
