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
| Closure pass | From `70c2e43` (29 October 2026). `make verify` on the committed tree of `1235494`: 15/15 stages (unit 503, integration 259, browser 33, including both scoring-label tests), on a dev stack restarted on that code with the VM clock in sync. The replay check reproduces held-out 36/40 and security 7/7, both modes, under `investigation/1.1.0`. Hosted CI run 40982031258 green on `1235494` (all four jobs). Later commits change only this record, STATUS and the release workflow's index check (YAML parsed; no gate runs it) |
| After the closure review | Local branch `feat/after-pr17` from `332c7d0` (30 October), not pushed while PR #17 is under review. `make verify` on the committed tree of `c521435`: 15/15 stages (unit 516, integration 260, browser 34). The replay check covers both frozen plans, and `eval-verify` checks all 34 ledger records. The VM clock was reset first, and the dev stack restarted on that code. Hosted CI runs once the branch is pushed |
| Environment | macOS host; PostgreSQL 16, Keycloak, SeaweedFS in Podman; no cloud account; no real model or customer system contacted |
| Review documents | supplied by the owner in the session; not committed (the repository is public) |

## Finding matrix

| ID | Finding | Status | Package | Evidence | Depends on |
|---|---|---|---|---|---|
| A01 | Malformed or empty invoice lines became a clean match, and the case could be resolved | Fixed, verified locally | 1 | See "Package 1" below | — |
| A02 | Duplicate PO line identifiers silently overwrote one another | Fixed, verified locally | 1 | See "Package 1" below | — |
| A03 | "Instability" was correctness instability: two different wrong actions counted as stable | Fixed, verified locally (scoring 2; v1 kept under scoring 1). Run pages name their scheme, workflow and purpose. An engineering re-run of the v1 cases is recorded under scoring 2 and `investigation/1.2.0` (regression evidence, not held-out), and browser tests cover both schemes on committed results | 4 | See "Package 4, metrics" below | — |
| A04 | Response size checked after buffering the whole body | Fixed, verified locally (purchasing source, task trackers, Ollama; a compressed body counts only when complete; replies the Anthropic and MCP SDKs read are bounded under their clients) | 3 | See "Package 3, transport bounds" below | — |
| A05 | Casework MCP admission weaker than the executor's; rebuilds outgoing text from parameters | Fixed, verified locally (real MCP, PostgreSQL, Keycloak) | 2 | See "Package 2" below | — |
| A06 | Verifiers load the private signing key; credential references not tenant-owned | Partly fixed, verified locally: public-only verifiers with key rotation, tenant-registered credentials; parser subprocesses get none of the service's secrets and are confined with Landlock (no other files, no TCP) and a seccomp filter (no new sockets: no UDP, DNS or Unix sockets), required when deployed; each container is given only its own secrets, and in the compose stack only bootstrap and the worker mount the delegation signing key. Not done: keys from a secret store mounted per role (staging work); the staging platform's kernel must offer Landlock, or the sandbox is waived there by setting `OPSBRIDGE_PARSER_SANDBOX=best_effort` | 3 | See "Package 3, keys and credentials" below | Staging (A11) for per-role secret mounts |
| A07 | Important fixes only in open PR #17 | Fixed: the owner merged PR #17 on 30 October (`4d2f1f6`) | 0 | CI on `main` green at `4d2f1f6` (run 41085903247, all four jobs) | — |
| A08 | No actual purchasing source | Blocked for live proof. Done locally: an export-file source held to the contract (hand-written exports only), and the single-source pilot's measures defined and implemented before any data (protocol `opsbridge-pilot/1`, `pilot-report`), with `export-check` to read a pilot export through its pack before the first case | 5 | See "Package 5, export source" and "Package 5, pilot measurement" below | Owner chooses a system or export, provides a real (sanitized) export or sandbox, and authorizes access |
| A09 | AI usefulness not measured on its actual task | Blocked for real-model evidence. Protocol prepared (`docs/evaluation-plan-v2.md`: measures, model identity, failures in denominators) | 6 | Mock-only results; no paid calls made | Model credential, budget and authorization |
| A10 | Labels by the same author; held-out set public | Blocked. Protocol prepared (`docs/evaluation-plan-v2.md`: independent labelling, adjudication records, family-sequestered split, sample sizes chosen in advance) | 6, 9 | — | Independent labellers and new data |
| A11 | Staging is a plan only | Blocked | 8 | `docs/deployment.md` plan | Cloud account, choice and spending authorization |
| A12 | `main` unprotected; no release promotion | Ruleset applied by the owner (5 November; its required checks are tested against CI's job names). Release workflow prepared, not run (tag equals every declared version; CI evidence read from the Actions API; the digest published is the digest qualified; SBOM, provenance attestation, verification; a pre-release tag is accepted and its image labelled experimental, and the packages declare `0.1.0-alpha.1`) | 8 | See "Package 8, governance and releases" below | Owner pushes the first tag, `v0.1.0-alpha.1`, on `main`'s head, `79992cd` (PR #30), where all three release checks pass, the release refuses fixable HIGH findings, and it builds, verifies and attests the evidence bundle |
| A13 | Replay compares next actions only; mock replay depends on rules-only trials running first | Fixed, verified locally: behavioural replay comparison; the cause (a finding-text defect) fixed as workflow `investigation/1.2.0`; v1 recordings replay with `investigation/1.1.0` in their declared order, and other orders fail closed | 4 | See "Package 4, behavioural replay" below | — |
| A14 | Operator workflow unvalidated | Partly: PR #17 ownership, views, settings sections, keyboard focus | 7, 9 | Browser tests | Operator sessions |
| A15 | Capacity and spend bounds unmeasured | Partly: one time budget for reading the source per investigation. Load figures, SLOs and spend accounting need staging | 3, 8 | See "Package 3, collection budget" below | Staging and the owner's expected load |
| A16 | Identity and privacy lifecycle incomplete | Partly: ended sessions and their ID tokens are now pruned, and a sign-out at the identity provider ends OpsBridge's sessions at once (back-channel logout, verified against Keycloak 26.8.1). Profile deletion, backups and object versions are not done | 3, 8 | See "Package 3, session lifecycle" below | The owner's deprovisioning policy |
| A17 | Image lacks the evaluation evidence the repository verifies | Done, verified in CI: summary-only in the image, full verification from the repository or from a reproducible evidence bundle that a release builds, verifies with its image and attests; the image check and bundle round trip pass on `main` at `77a515b` | 8 | See "Package 8, evidence packaging" below | — |
| A18 | Large modules; status documents drift | Ongoing: README status refreshed 2026-11-02 against this record; each package updated STATUS, this record and the runbooks. Shared contracts extracted where they removed duplication (`domain/validation.py`, `services/execution_admission.py`, `connectors/http_transport.py`); no broad refactor | all | — | — |

Earlier findings keep their own history in `docs/review-findings.md`: F01–F18 (review of
22 October) and X1–X7 (found along the way). A01–A18 do not replace them; where they overlap
the earlier entry is referenced.

## Review of 5 November 2026 (N01–N09)

A later review of `main` at `79992cd` listed concrete defects N01–N09. They are tracked here
apart from A01–A18, which keep their history. No earlier finding was reopened: none of their
invariants failed. Each finding was reproduced at the head before it was changed, and its
regression failed for the stated reason before the fix.

| Item | Value |
|---|---|
| Reviewed `main` | `79992cd` (5 November 2026), CI run 41980269125 green; Python `0.1.0a1`, frontend `0.1.0-alpha.1`, unpublished |
| Work | `feat/opsbridge-foundation`, from `79992cd` (working tree clean); PR #36 |
| Ruleset | Live ruleset 26598146 on `main`, active: pull requests only, the four CI jobs required on an up-to-date branch, review threads resolved, no force push or deletion, administrators bypass only by merging a pull request, **0 approvals**. It is not enforced independent review |
| Gates run here | Unit, integration (PostgreSQL 16, Keycloak and MCP in Podman), canary, receipts, replay check, lint and types. **Not here:** the image build (about 3 GB of disk free), Landlock and seccomp (macOS), OCR (no tesseract), browser tests (not run this session). Those run in hosted CI and are named, not counted as passed |
| Review documents | `PRODUCTION_REVIEW.md` and its file inventory were not supplied. The findings are taken from the brief, which does not define N09 |

| # | Finding | Reproduced at `79992cd` | Changed | Invariant now | Evidence | Remaining |
|---|---|---|---|---|---|---|
| N01 | Duplicate JSON members | Yes: an invoice line with `quantity` 999999 and then 10 mapped as 10 and **matched** end to end | `safejson.py` | An object naming a member twice (as decoded, NFC) is refused whole; no record is built | `test_safejson.py`, `evidence/test_duplicate_json_members.py`, listing, manifest and model-output tests, `test_an_invoice_giving_a_quantity_twice_neither_matches_nor_resolves`; canary 64/64, 34 receipts verify, replay reproduces | OpsBridge's own recorded files are read with `json`; a scan found no duplicate in them |
| N02 | Request body limits trusted the framing | Yes: an HTTP/2-framed 5,000-byte body was read whole against a 1,000-byte limit; chunked bodies got 411; a trickled body was accepted | `api/body_limits.py` (new), `middleware.py`, `app.py`, `errors.py`, `config.py` | Received bytes are counted whatever the framing and refused at the chunk that passes the limit; the body has a time budget (408); chunked bodies within the limit are accepted | `tests/test_request_body_limits.py` (12, incl. a uvicorn socket: the 413 arrives while the client is still sending), `test_hardening.py` | The edge proxy's own limits, at staging |
| N03 | Source clock and freshness | Yes: an export dated a year ahead gave a completed, actionable result; one 2 minutes ahead was recorded as read in the future | `connectors/exports.py`, `workflow/investigation.py`, `services/freshness.py`, `workflow/worker.py`, `config.py` | An export more than `source_clock_skew_seconds` ahead is refused; times are RFC 3339 with a zone; the data's age is the earlier of the source's time and the host's; the window is never later than the stored time allows | `contracts/test_export_source.py` (injected clock), `workflow/test_source_window.py`, `test_an_export_dated_ahead_of_the_clock_is_not_read` | A live source asserts no time; none needed a migration (JSONB) |
| N04 | Evidence bundle extraction unbounded | Yes: a member given twice, a manifest not first, a broken checksum and data after the end were accepted; no cap on expansion or any file | `evaluation/bundle.py` | One bounded pass: strict manifest first, canonical listed regular files given once, per-file, header and expansion limits, checksum read to the end, nothing extracted on refusal | `evaluation/test_evidence_bundle.py` (26, reduced limits, small archives); the reproducible round trip still verifies | Limits sized from the current bundle |
| N05 | Pilot report not historical | Yes: an approval made after the cut-off counted as approved as drafted, and as a decision reached | `pilot/cohort.py` (new), `pilot/collect.py`, `pilot/measures.py`, `pilot/reference.py`, `cli.py`, `persistence/db.py`, migration 0023, `docs/pilot-plan.md` | Protocol `opsbridge-pilot/2`: a frozen cohort; only what existed at the cut-off, in one read-only, repeatable-read snapshot; every enrolled invoice accounted for; incomplete when a decision is missing; coverage apart from accuracy; provenance in the report | `tests/pilot/` (48), `integration/test_pilot_report.py` (4: later events change nothing; a concurrent commit is unseen and is seen under read committed, a mutation that fails the test), `test_migration_0023.py` | No pilot data, cohort or reviewer; no `/1` result existed to show beside `/2` |
| N06 | Credential ownership incomplete | Yes: a staging connection without a reference received the deployment token; `a-b` and `a_b` both read one secret | `services/connections.py`, `config.py`, `api/routes/admin.py` | Deployed, each GitHub connection names a credential registered to its tenant and repository; colliding references are refused; a tenant is shown only the repositories it can use | `tests/test_connection_credentials.py`, `integration/test_credential_registry.py` | Per-tenant secrets at staging; a revoked credential is never reused, so its possible writes go to a person |
| N07 | Total deadlines not truthful | Yes: trickled headers held a 0.6 s purchasing read for 7.4 s and a 0.5 s model request for 7.4 s; an MCP call to a silent server took 10.3 s against 0.3 s; late-ending replies were accepted | `connectors/http_transport.py`, `connectors/sdk_transport.py`, the connectors, `agents/adapters.py`, `mcp_services/client.py` | Every socket wait is capped at the time left of one deadline; the deadline is checked after the headers and at the end; an MCP call ends within its total | `test_http_transport.py`, `test_sdk_transport.py` (fake clocks and a scripted server, wall time); full integration | Name resolution is not capped; connectors no longer use environment proxies |
| N08 | Offline replay egress | Yes: a client made before the guard reached a loopback proxy, which received the CONNECT; proxy variables stayed set; live modes were not refused | `evaluation/egress.py`, `evaluation/bench.py`, `cli.py`, `evaluation/canary.py`, `evaluation/local_server.py` | Proxy variables are removed and their ports refused while the guard holds; only registered loopback ports; live modes refused before a run | `evaluation/test_offline_proxy_guards.py`; replay check reproduces every recorded run | A Python-level guard, not a sandbox |
| N09 | Not defined in the supplied brief | Not reproduced | Nothing | — | — | The owner to name it |

Also from the brief's Phase G, done locally:
- `ops.prune_sessions` validates its own period (migration 0024); `'-100 years'` deleted live
  sessions before.
- docs/account-lifecycle.md (A16's mechanics, and the owner's six decisions).
- `tests/test_docs_consistency.py` (named commands, settings and release commits exist and
  agree; it found `make eval-verify` missing).
- From Phase E, in the repository only:
  - the release accepts only its own workflow's attestation of the tag, from a GitHub-hosted
    runner;
  - rulesets that make release tags immutable and administrator-created are prepared for the
    owner, not applied.

Found along the way: on 5 November a new HIGH advisory in Debian's `apt` (CVE-2026-109518,
fixed in 2.6.2) stopped CI's image gate. The runtime image pins `apt` and `libapt-pkg6.0` to
2.6.2 beside OpenSSL, and the image job passed on it (run 42082021608).

Phases D–G need what only the owner can give: a source, export and reviewer; a cloud
account and spend ceiling; a model budget; operator sessions. Nothing was assumed for them
(see "Needs the owner" in the work plan).

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

**Browser coverage** (closure pass, 29 October): no browser test asserted either scheme's
labels, and the first gate after A03 ran its browser stage against the stack started before
it. The run page now names the scheme the API returns (it did not show it). In
`e2e/phase7.spec.ts`: the committed held-out record shows `opsbridge-scoring/1` and, in both
modes, its flip-rate figure with the note on what it measured; a scoring-2 run is covered for
rendering only, because no scoring-2 result is committed: the test serves the real held-out
response with its instability in the scoring-2 shape (`metrics.instability_2`) and checks
per-axis disagreement and that the scoring-1 note is absent. Both passed in the browser stage of
`make verify` at `1235494` (33 browser tests, against a dev stack restarted on that code).

**Derived reading of the recorded v1 runs under scoring 2** (`opsbridge eval-rescore`, prints
only): no disagreement on any axis in any recorded split or mode (held-out 0/40, validation
0/20, security 0/7, both modes), all complete. The metric was wrong, but it hid nothing in
these results, because the rules and the mock model are deterministic.

**Engineering re-run** (30 October, after the closure review): to bring the committed
evidence in line with the current code, `opsbridge-bench-v1-rerun` re-runs the public v1
cases under scoring 2 and `investigation/1.2.0` with the mock model. Held-out is 36/40 and
the other splits are perfect in both modes, with no disagreement on any axis; the verdicts
equal v1's (docs/evaluation-results.md).
- **Labelled as regression evidence.** The plan carries a purpose, shown with every run, and
  its own acceptance policy reports the held-out gate as not evaluated, because those cases
  are not held out. Plans now name the policy they are judged by; v1 keeps its own, and
  verifies and replays unchanged. A policy declared for another benchmark is refused.
- **Covered in CI.** `eval-replay-check` replays every frozen plan, so `make verify` and CI
  cover the re-run without changes.
- **Browser coverage on a committed result.** The intercepted scoring-2 browser test is
  replaced by tests on the re-run's real held-out record: per-axis disagreement, workflow,
  purpose, and the gate not decided.
- **Evidence.** `tests/evaluation/test_plan_acceptance.py` has 6 tests, all of which failed
  before; `tests/integration/test_evaluation.py` has the re-run API test.

**Not done**: no `opsbridge-bench-v2` is frozen. It should come with the independently
labelled corpus of A10; freezing v2 on the public v1 cases would invite out-of-sample claims
they cannot support.

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

**Compressed bodies** (closure pass): reproduced: a gzip or deflate body without its 4-byte
trailer (the whole document decodes, the checksum never arrives), or with bytes or a second
gzip member after its end, was accepted, and a damaged stream escaped as `zlib.error`
outside the error contract. Six damaged 201 replies to a create were returned as tasks. Now
a compressed body counts only if its stream reached its own end with nothing after it. A
second member is refused: HTTP servers send one, and accepting more would let bytes after
the checked end reach the parser. A response with no body bytes (204, 304) has no stream to
check. Every failure is `CorruptEncoding`, an `httpx.DecodingError`, so a create stays
ambiguous, a read-back or lookup is unavailable, and a purchasing read is unavailable
(retryable; nothing is concluded about the record). `tests/test_http_transport.py` now has 36
tests, 23 of them new: five kinds of damage in both encodings, an empty compressed 204,
tracker and GitHub creates with each damage, a read-back and lookup, and a purchasing record.
20 of the new tests failed before; the other three already passed (the empty 204, and creates
whose body was cut mid-stream and so was not valid JSON either).

**Responses the SDKs read themselves** (30 October): the Anthropic SDK and the MCP client
read through their own `httpx2` clients, out of this module's reach.
- **Reproduced:** given a 64 MiB reply, the Anthropic SDK read all of it and handed it back as
  text, and the adapter then failed with an `AttributeError` instead of falling back. The MCP
  client read all 64 MiB of a JSON reply before failing to parse it.
- **Fix** (`connectors/sdk_transport.py`): a transport under each client counts bytes as they
  arrive and stops past the limit, refusing a declared oversize first. It turns the request's
  read timeout into a total deadline for the whole response. It asks for unencoded replies
  and refuses a compressed one, which the client above would inflate without a bound.
  - **Limits:** 8 MiB per model reply, 1 MiB per MCP response.
  - **Error contract:** errors subclass `httpx2`'s, so they reach the existing handling.
    The adapter reports the model as unavailable or timed out, and the rules-only path takes
    over; an MCP create stays ambiguous.
  - **Non-message replies:** a 200 that is not a message is now "model unavailable".
- **Evidence:** `tests/test_sdk_transport.py` has 7 tests. Both oversize cases read up to the
  limit and no further. The adapter tests keep their assertions, and the 18 real-MCP
  integration tests pass over the bound.
- **SSE:** MCP's SSE streams are held to the same limits. Each call opens a session shorter
  than its deadline, so that cuts nothing legitimate.
- **Not covered:**
  - the SDKs' streaming APIs (OpsBridge uses none);
  - responses already held in memory (test transports only), which are checked after the
    fact and decoded by httpx.

  Total source-collection time is bounded per request (pages × deadline), not as one budget.

**Intermittent timeout test, diagnosed** (closure pass): the test was
`tests/contracts/test_github_connector.py::test_ambiguous_failures_never_claim_absence[timeout_after]`.
It failed 3 times in 73 whole-file runs. Once was in a gate run during which the Mac slept;
the other two were around the VM clock repairs, which followed the Mac sleeping. Every run
was then traced, recording:
- per create: elapsed time, configured deadline, outcome and exception chain;
- per server request: arrival, fault queue before and after, issues created and response
  time.

No timing variation alone reproduced it:

| Condition | Runs | Ended in `AmbiguousFailure(ReadTimeout)` | Client elapsed |
|---|---|---|---|
| Idle | 100 | 100 | 1.001–1.003 s |
| Full CPU load (20 busy processes, 10 cores) | 60 | 60 | 1.001–1.003 s |
| Process frozen (SIGSTOP) for 4.5 s across both deadlines | 30 | 30 | 4.6–5.4 s |

A timer armed late in a stalled uvloop iteration still waited its full 3 s.

**Cause**: the fake delayed its reply with a 3 s timer on uvicorn's event loop, uvloop. On
macOS uvloop's clock is `CLOCK_MONOTONIC`, which counts the time the host slept; on this Mac
it ran 730,000 s ahead of `time.monotonic()`. The client's 1 s deadline and its socket timeouts
run on `mach_absolute_time`, which does not count sleep. When the host slept during the delay,
the reply arrived within the deadline by the client's clock, and the connector returned the
issue the fake had really created. The logged failure fits: the fake held issue 1, and the run
was about 2 s shorter, with no 3 s handler left to wait for. Moving only the server's clock
forward 5 s reproduced it deterministically: 5 of 5 creates returned the issue after
0.11–0.13 s.

**Contract**: not violated. The connector received a genuine 201, within its own deadline,
for an issue that exists, and returned it; it never claimed absence or invented a task. The
test assumed 3 s on the server's clock is later than 1 s on the client's, and a host sleep
breaks that.

**Fix** (test fixtures and the simulator only; the invariant is unchanged and the test is
stricter):
- The fake commits the issue and holds the reply until the test is done.
- The test also asserts the remote effect (the issue exists after `500_after` and
  `timeout_after`, not after `503_before`) and that the cause is a timeout.
- `test_a_late_reply_stays_late_when_the_server_clock_jumps` failed against the old fake
  (`DID NOT RAISE`) and passes now.
- The simulator's `timeout_after_commit` and `delay` faults had the same dependence (a 3 s
  delay against the integration stack's 2 s deadline). They now wait by `time.monotonic()`;
  `tests/contracts/test_simulator_faults.py` has 2 tests, both failed before.

With the fixed fake, all 40 traced whole-file runs passed. On Linux (CI and the image) both
clocks exclude suspend.

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

**Parser subprocesses** (2 November, before the pilot reads real invoice files):
- Reproduced: the PDF worker inherited the service's whole environment, and tesseract was
  given it explicitly (`{**os.environ, …}`). In a deployment that environment holds the
  database URL, the delegation private key and the model and tracker tokens, so a flaw in
  pypdf, Pillow or tesseract, reached through a hostile file, would have found them. The
  worker also ran as `python -m` from the caller's working directory, which Python puts first
  on the import path. It imported a `base64.py` or `json.py` from there, so `export-check` run
  from inside an export would have executed one planted in it.
- Fix: both get only `LANG`, `LC_ALL`, `LC_CTYPE`, `PATH` and `TESSDATA_PREFIX` (tesseract
  also `OMP_THREAD_LIMIT=1`), and the worker runs in Python's isolated mode (`-I`).
- Evidence: `tests/evidence/test_parser_isolation.py` (3, all failed before). Removing any
  one of the three protections fails its test. In the app image, as uid 10001 on a read-only
  root, tesseract 5.3.0 still read the scanned fixture under the restricted environment.

**Secrets per container** (2 November):
- Reproduced: one shared environment block gave every application container every secret.
  The simulator and both MCP services held the migrator's database password, which owns
  every table, as well as the API's session and client secrets and the worker's client
  secret.
- Each container now gets only what its process reads: its own database role, plus the
  API's OIDC client and session secrets, the worker's client secret, and the simulator secret
  where simulator records or tasks are reached. Only the one-off bootstrap holds the
  migrator's password.
- The session-secret rule moved from the settings every process loads to the API's startup:
  it is the API's alone, and in staging it would have refused a worker that holds none.
- Evidence: `tests/test_compose_secrets.py` (7; 6 failed before) and
  `tests/test_deployment_settings.py` (3 added, failed before). The containers demo, run with
  the split secrets, did all of this:
  - signed in through Keycloak;
  - ran an investigation through the worker, the records service and the simulator
    (`discrepancies_found`);
  - executed an approved follow-up task through the casework service (`confirmed`);
  - ended the session when Keycloak signed the person out.

**Key volumes** (3 November):
- Reproduced: every application container except the simulator mounted `opsbridge_appdata`,
  where `init-keys` wrote the delegation signing key. Either MCP service, which only
  verifies, could read it and sign a delegation for any tenant, principal and tool.
- Fix: the key has its own volume. Bootstrap mounts it to create the key; the worker, the one
  signer, mounts it read-only. Bootstrap copies the public key to a second volume, which the
  MCP services mount read-only and which is all they mount. The image makes the new mount
  point, owned by the service user. Docker gives a new volume the owner of the image's
  directory, or root's where the image has none.
- Evidence: `tests/test_compose_secrets.py` has 9 new cases; 6 failed against the shared
  volume. The mount-point case failed once the compose file named the new mount and before
  the image made it. CI's image job checks under Docker that each mount point is writable.
  Podman gives a new volume the container's user, so a local run does not show the fault.
  The containers demo was run on the local image with only the new directory added:
  - Of the running services, only the worker had the private key. The MCP services had only
    the public key, the same as the worker's.
  - An investigation ran through the records service (`discrepancies_found`).
  - An approved task ran through the casework service (`confirmed`).
  - A Keycloak sign-out ended the session.
- A stack created earlier keeps the old key in `opsbridge_appdata`, where the API can read
  it. Nothing trusts it now, and docs/deployment.md gives the command that removes it.

**Parser sandbox** (4 November):
- Reproduced: with the environment restricted, a flaw in pypdf, Pillow or tesseract reached
  through a hostile file still found every file the service can read and its network: the
  worker's delegation signing key, other tenants' artifacts, the database and the MCP
  services.
- Fix (`opsbridge/evidence/sandbox.py`): the PDF worker confines itself with Landlock before
  it reads the file. It may then read only its own code and the system libraries; it writes
  nothing and, from Landlock ABI 4 (Linux 6.7), can neither bind nor connect TCP. Tesseract
  is started through a launcher that confines it to the system files and its one page.
- Policy: `OPSBRIDGE_PARSER_SANDBOX` is `required` when deployed. A parser then refuses a
  file it cannot be confined for, before reading it, and the API and the worker refuse to
  start unless a confined probe could neither read a planted file nor connect to a
  listening port. Development is best effort, since macOS has no Landlock. The Linux
  containers are confined anyway.
- Evidence: `tests/evidence/test_parser_sandbox.py` (8). Where Landlock is available, a
  confined probe reads no planted file and reaches no listening port, yet still loads its
  code; the PDF worker reports its confinement; the launcher reads only the page it is
  given. CI requires these to run (`OPSBRIDGE_REQUIRE_SANDBOX=1`), and its image job and the
  release run `parser-sandbox-check` in the image as compose runs it. Removing the worker's
  confinement, the TCP rules, the launcher's confinement or either startup check each fails
  a test. In the hardened app container (Landlock ABI 7), the evidence tests pass, including
  the real-engine OCR test, now confined. Hosted CI (run 41932656486): the runner offers
  Landlock ABI 8, all the confined tests ran (616 unit tests, only the macOS-only case
  skipped), the integration and browser jobs parsed their PDFs confined, and
  `parser-sandbox-check` held in the image under Docker. `export-check` runs offline in the
  image with the sandbox required (docs/pilot-plan.md).
- The socket filter (5 November). Landlock left UDP (so DNS) and Unix sockets with a path
  open to a confined parser: the probe sent a UDP datagram and connected to a Unix socket.
  Neither the PDF worker nor tesseract needs a socket, so a seccomp filter installed after
  Landlock refuses `socket()` and `io_uring_setup()` (io_uring can open sockets without
  `socket()`) with EACCES. It kills a call made through another ABI (32-bit or x32), which
  would get around the filter's numbers. `required` now asks for both layers. The probe
  runs unconfined as a control, then with each layer alone and with both:
  - the control reaches the file, TCP, UDP and the Unix socket;
  - Landlock alone keeps the file and TCP;
  - the filter alone keeps every socket and io_uring;
  - both keep everything.

  So neither layer can be lost behind the other. In the hardened app container, removing
  the filter, leaving io_uring out of it, or dropping Landlock's TCP rules each fails a
  test.

**Not done** (staging, A11): keys from a secret store, mounted per role; the compose stack
separates them with volumes on one host. The parser sandbox's socket filter refuses new
sockets and io_uring only, not other system calls. Whether the staging platform's kernel
offers Landlock is to be checked there (docs/deployment.md); where it does not, the sandbox
is waived only by setting `OPSBRIDGE_PARSER_SANDBOX=best_effort`.

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
`eval-verify` still checks all 26 records' artifacts (34 now; gate stage unchanged). The CI
image job runs both commands inside the built image, for every pull request and every commit
to `main`.

**Evidence bundle** (2 November):
- `opsbridge eval-bundle` packs the ledger, its signatures, its public key and the 127 files
  its 34 records bind into one archive, with a manifest of their SHA-256. The archive is
  reproducible: built on macOS, inside the Linux image and on the hosted runner (for PR #27
  and on `main` at `77a515b`, run 41568201604) it had the same SHA-256, `2f52004e…`.
- `eval-verify --bundle FILE` refuses a bundle in any of these cases:
  - an unsafe member: absolute, `..` or a link;
  - a file that differs from the manifest;
  - a ledger, signatures or key other than the installation's own.

  It then verifies every record's artifacts from the bundle alone.
- The release builds the bundle in the candidate image from the tagged checkout, and
  verifies the candidate with it before anything is attested or tagged. It keeps the bundle
  with the run, attests it, and gives its SHA-256 in the run summary. CI's image job runs the
  same round trip.
- Evidence: `tests/evaluation/test_evidence_bundle.py` (8). Skipping the manifest digests or
  the ledger comparison each fails a test. If the manifest is rewritten to match a changed
  file, the ledger's own digest still catches the change. Run locally in the image: chain
  intact, 34 signatures valid, 34 records ok.

**Not done**: the bundle is a workflow artifact, kept for the repository's artifact
retention (90 days by default), not a permanent release asset. Keep the copy deployed with
each image, or publish it as a release asset (needs `contents: write` and the owner's
decision).

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
`fur-security-304` and fails closed; in reversed order all seven cases fail. Cause: the
workflow the v1 runs were recorded with, `investigation/1.1.0`, named only the problems found
by the current run's read, so the invoice-extraction finding, and the model request built
from it, depended on which trial investigated first. The v1 recordings are replayed with
that workflow (the frozen plan names none, which means 1.1.0) in their declared order;
`tests/integration/test_replay_mode_order.py` checks that the frozen order reproduces and
other orders fail closed rather than differ quietly. Three validation cases recorded fresh
replay in any order (no such document among them).

**Evidence**: `tests/evaluation/test_reproduce.py` (3 new), `test_replay_mode_order.py` (3).

**The finding-text defect, fixed as a workflow version** (closure pass): under 1.1.0,
`fur-security-304` (a line amount that disagrees with quantity × price, found when the record
was mapped) named its reason on the case's first investigation only, and `fur-security-303` (an
image-only invoice, found by the parser) never: "could not be read reliably ()". The earlier
STATUS entry described only the first. `investigation/1.2.0` names the reason stored with the
document's newest version on every run ("invoice INV-9303: no extractable text layer (likely
a scanned image); OCR is not enabled"). The version is explicit at each level:
- each run records the workflow it follows (`workflow_runs.workflow_version`, returned by the
  API) and runs that behaviour; a run for a version the worker does not implement is refused,
  retryably;
- a frozen plan records the version its trials run (`workflow_version`; v1 has none, which
  means 1.1.0), and `verify` refuses a version this code does not implement;
- benchmark recording and replay use the plan's version, and new results record it.

No recording, cassette or result changed; the replay stage of the gate reproduces the
recorded v1 runs under 1.1.0. Evidence:
- `test_reinvestigation_reason.py`: through the API, two investigations of the same
  unreadable invoice give the same finding, naming the stored reason; failed before.
- `test_reinvestigation_regression_cases.py`: regression evidence on the public v1 security
  cases 303 and 304, not a measurement (not recorded, not scored). Under 1.2.0 every
  investigation names the reason in either mode order; under 1.1.0 the historical behaviour
  above.
- `tests/evaluation/test_comparability.py`: plan workflow versions.

Recorded since: `opsbridge-bench-v1-rerun` ran every split with 1.2.0 (Package 4, metrics).
Its security recording's model requests name both reasons where v1's said "()".

## Package 8, governance and releases (A12)

**Prepared** (repository administration and publishing are the owner's; the owner applied
the ruleset on 5 November):
- `infra/github/main-ruleset.json`: pull requests only, the four CI jobs required on the
  latest commit, review threads resolved, no force push or deletion; administrators may
  bypass only through a pull request (break-glass). Required approvals 0 while there is one
  maintainer. Applied as ruleset 26598146; read back through the API it matches the file,
  apart from defaults GitHub adds (every merge method allowed). `tests/test_workflows.py`
  checks that it requires exactly CI's job names, strictly, on the default branch. How to
  read and replace it is in docs/deployment.md.
- `.github/workflows/release.yml`, corrected in the closure pass. Before, it checked CI
  check runs by name only (any app or workflow can create one with any name), then built a
  fresh image, so what it published was never an image anything had tested. CI publishes
  nothing and also runs on pull requests, so it has no digest to promote; the release
  qualifies the exact image it publishes instead:
  - it runs for a version tag only;
  - it refuses a tag that disagrees with any declared version (`pyproject.toml`,
    `opsbridge.__version__`, `uv.lock`, `package.json`, `package-lock.json`);
  - it refuses a commit unless a successful run of this repository's `ci.yml`, started by a
    push to `main`, qualified it with every required job succeeding. The run is looked up
    through the Actions API by workflow file, event, branch, repository and commit
    (`scripts/release_check.py`);
  - it builds once from the locks and pushes only a candidate tag;
  - it runs CI's image checks on that digest: non-root, CLI, `eval-verify` summary and
    partial exit, API health, SBOM, and fixable critical or high vulnerabilities;
  - it attests and verifies the digest;
  - only then does it give the same digest the version tag, without rebuilding, refusing if
    the tag resolves to anything else.

  Actions are pinned to commits of releases at least two weeks old.

**Pre-releases** (30 October, closure review step 3). The first release is to be explicitly
experimental, but the workflow accepted only `vMAJOR.MINOR.PATCH`, so it could only have
been published as a plain `v0.1.0`, a tag that reads as an ordinary release. It now also
accepts `vX.Y.Z-alpha.N`, `-beta.N` and `-rc.N`:
- npm files must declare the tag's spelling (`0.1.0-alpha.1`) and Python files the PEP 440
  spelling (`0.1.0a1`); the plain version does not match a pre-release tag, nor the reverse;
- the image carries an OCI description, and the run summary repeats it: "experimental
  pre-release (artifact delivery only; not qualified for production use)". It also carries its
  source repository;
- the packages now declare `0.1.0a1` and `0.1.0-alpha.1` (0.1.0 was never tagged), and
  `release_check.py versions --tag v0.1.0-alpha.1` passes on this tree.

docs/deployment.md gives the tag command and the commands that check the published digest,
attestation and runtime after a release.

**Evidence**: `tests/test_release_check.py` has 17 tests (3 added for pre-releases; one
extended to refuse other suffixes), covering:
- a qualifying run;
- refused look-alikes: another commit, a pull-request event, another branch, a workflow file
  with the same job names, a fork, another repository, a failed or unfinished run;
- a failed, missing or other-run job;
- malformed and mismatched tags, including `-rc1`, `-experimental.1` and build metadata;
- pre-release tags, each ecosystem's spelling, and the experimental description;
- every version declaration read, and one left behind by a bump caught;
- job names that match `ci.yml`.

Run read-only against this repository, `main`'s head `468dae7` qualifies and the PR head,
never pushed to `main`, is refused. The five declarations agreed on 0.1.0 then. On 1 November,
`main`'s head `b63c5fc` (PR #20; CI run 41277454673) passed all three checks for
`v0.1.0-alpha.1`: the versions, the experimental description and CI. On 2 November, so did
`16951dc` (PR #23; CI run 41409288359), which also gives parser subprocesses no secrets. It
still ships pypdf 6.20.1 and an OpenSSL with fixable HIGH findings (below), so the first tag
belongs on PR #25's merge commit instead. That is `23b2f84`: its CI run on `main`
(41485053120) passed all four jobs, and all three checks pass on it. On 3 November so did
PR #27's merge commit `77a515b` (CI run 41568201604), whose release also builds, verifies
and attests the evidence bundle (Package 8, evidence packaging). Each merge since has
qualified too, the latest on 5 November: `79992cd` (PR #30; CI run 41980269125), which also
confines parser subprocesses. The first tag belongs there. The ruleset's check names match
CI, and a test keeps them matched. The workflow has not run (no tag), so its first use is
its first test, and no attestation exists yet. The promotion relies on `docker buildx
imagetools create` keeping a single image index as it is. The step therefore refuses, before
tagging, a candidate that is not an index; the build's provenance attestation makes it one.
It checks again after tagging that the tag resolves to the qualified digest. The candidate
tag stays in the registry.

**Vulnerability gate** (2 November). CI and the release stopped only fixable CRITICAL
findings. A report-only HIGH scan of the image (run 41418268617) found twelve, all fixable:
- pypdf 6.20.1: ten denial-of-service findings, fixed by 6.21.4, which is now locked. Pilot
  PDFs go through it; the worker's timeout and memory cap bounded these but did not remove
  them.
- OpenSSL 3.0.22-1~deb12u1 in the base image: CVE-2026-84782, fixed by 3.0.22-1~deb12u2,
  which is now pinned in the image.

Both CI and the release now refuse fixable CRITICAL or HIGH findings. With pypdf 6.21.4, the
unit, integration and canary gates pass, and both frozen plans still replay case by case.

**Not done**: a staging deployment that promotes the attested digest, and rollback rehearsal.
MEDIUM findings and unfixed ones are reported nowhere.

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

## Package 3, session lifecycle (A16, in part)

**Before**: a session that expired, went idle or was revoked stayed in `user_sessions` for
good, holding the identity provider's ID token (name, e-mail and other claims); nothing
deleted them.

**Now**: the worker deletes sessions that ended more than `session_prune_after_hours` (24)
ago, on its retention schedule, through `ops.prune_sessions` (migration 0021): SECURITY
DEFINER, owned by the NOLOGIN role `opsbridge_session_pruner`, which may only read and delete
sessions, while the worker may only execute the function. The role is created by the
database initialization script (the migrator cannot create roles); 0021 stops with that
instruction if it is missing. Downgrade and upgrade rehearsed on the test database.
`tests/integration/test_session_pruning.py`: an active session and one ended five minutes ago
stay; expired, idle and revoked sessions three days old go. On the development database the
worker's first run deleted 2,030 ended sessions accumulated from two weeks of browser-test
sign-ins, each holding an ID token.

**Back-channel logout** (2 November). Ending a removed person's live sessions does not depend on
the deprovisioning policy; what to delete afterwards does.
- Reproduced against the local Keycloak 26.8.1: an administrator signed `furn-auditor` out at
  Keycloak, and their OpsBridge session still answered 200 ten seconds later; it would have
  lasted up to its idle or absolute limit (30 minutes, 8 hours). Disabling the account sends
  nothing and ends no session even in Keycloak; signing the person out sends one logout token
  per session (RS256, `typ: logout+jwt`, `sub`, `sid`, `jti`, `iat`, `exp`, the event).
- Fix: each session records the identity provider's session id from the verified ID token.
  `/auth/backchannel-logout` (OpenID Connect Back-Channel Logout 1.0) accepts a logout token
  only when it is signed by a key the provider publishes, with an asymmetric algorithm, for
  this issuer and client, is current, and carries the event and no nonce. It then revokes the
  live sessions it names through `ops.revoke_idp_sessions` (migration 0022: SECURITY DEFINER,
  owned by the NOLOGIN role `opsbridge_session_revoker`, which reads no tokens and may set only
  `revoked_at`; the API alone may call it). Sessions recorded before 0022 have no session id
  and end with any logout of their person. A token naming an unknown key fetches the keys
  again, at most once a minute. The realm file, `make identity-sync` and the compose file
  configure the local Keycloak; an unreachable API breaks neither kind of sign-out at Keycloak
  (checked). `opsbridge end-sessions` ends a person's sessions when a logout did not arrive,
  since Keycloak does not retry. docs/operations.md has the runbook.
- Evidence: `tests/auth/test_backchannel.py` (24: the accepted token and each refusal) and
  `tests/integration/test_backchannel_logout.py` (8). Against Keycloak, a sign-out ends the
  person's sessions and no one else's (failed before), and ending one of two Keycloak sessions
  ends only the OpsBridge session opened from it. Recording no session id, dropping the
  fallback for sessions recorded without one, accepting a nonce, or removing the refetch limit
  each fails a test. 0022 was downgraded and upgraded on the test database.
- In the browser (added the same day): a page open when its session ended kept its cached
  session for up to a minute and showed error panels. Now any 401 makes the page check the
  session again, and it says "Your session has ended" and offers sign-in.
  `frontend/src/App.test.tsx` (2): the ended session fails without the re-check. Without the
  guard that skips the session check's own 401, the page refetches until it runs out of
  memory.

**Not done**: deleting user profiles when an account leaves; backups and object versions.
These need the owner's deprovisioning policy. Identity providers without back-channel logout
keep only the idle and absolute limits, and a replayed logout token can end sessions again
until its `exp` (`jti` is not stored).

## Package 5, export source (A08's first step, not proof of any real system)

**Added** (`connectors/exports.py`): a purchasing source read from an export, a directory per
organization with the record files and an immutable manifest (`opsbridge-export/1`), held to
the `purchasing-source/1` rules:
- absence only when stated, so a partial export's missing record is unknown and goes to review;
- completeness only when declared;
- withdrawals listed.

Every file is checked against its SHA-256. An export is refused for a path outside it, a
repeated or conflicting identity, a record modified after `extracted_at` (a mixed snapshot),
an unknown schema or a malformed manifest. The investigation records the export's
`extracted_at` as its read time, so an old export cannot be acted on after the age limit,
however recently it was read. `OPSBRIDGE_PURCHASING_EXPORT_DIR` switches investigations from the
simulator to exports (`build_investigation_deps`). Production still refuses to start: no real
source has been validated.

**Evidence**: `tests/contracts/test_export_source.py` (15). Disabling the digest check, the
mixed-snapshot check, or partial-export absence each makes a test fail.
`tests/integration/test_export_investigation.py` (2): a hand-written IT-pack export of
PO-2001, INV-7001 and a receipt of 8 gives the same quantity shortfall as the simulator, and a
two-day-old export cannot be proposed on. Using the read time instead of `extracted_at`
makes both fail.

**Not done**: any customer export or system. The fixtures are hand-written, not captured
from a real export; a real one needs its own sanitized fixtures, a pack mapping for its
fields, and a decision on how exports are delivered and kept.

## Package 5, pilot measurement (A08, A14; prepared, no pilot run)

**Why.** The closure review's step 5 is one pilot on a real purchasing source, with an
authorized sanitized export and one AP or procurement reviewer. It measures extraction
completeness, investigation accuracy, approval corrections and time to resolution against
the existing manual process. OpsBridge recorded the facts those measures need, but nothing
defined or computed them. Nothing recorded the reviewer's independent decision or the manual
process's timings either. Defining a measure after the data arrives would invite choosing
the one that looks best, so the definitions and the report come first.

**Added.**
- Protocol `opsbridge-pilot/1` in docs/pilot-plan.md ("First step: a single-source pilot"):
  - sample fixed in advance, with arms alternating in blocked order;
  - the reviewer's decision recorded before intake, which is the only blind case;
  - the reference file's columns;
  - the four measures, with failures in their denominators;
  - the limits.
- `opsbridge/pilot/reference.py`: the reference file, read strictly. Every problem is
  reported with its line, and OpsBridge's own decisions and times can't be entered by hand.
- `opsbridge/pilot/measures.py`: the four measures.
  - Blind and not-blind accuracy are reported apart.
  - A referenced case that was never investigated counts as wrong.
  - Approval corrections compare what was approved with the investigation's draft.
  - Open cases are counted in the times.
- `opsbridge/pilot/collect.py` and `opsbridge pilot-report`: one tenant's cases opened in a
  window, read under its row-level security in a read-only transaction.
- `opsbridge/evaluation/verdict.py`: an investigation is read as a verdict by the same
  function the benchmark scores with. Both frozen plans still replay case by case.
- `opsbridge/pilot/export_check.py` and `opsbridge export-check`: before the first case,
  every record of the export is read through its pack, the way an investigation does, with
  nothing stored. An investigation reads one record at a time, so without this a defective
  file or a mapping gap would surface only in the case that needs it, and count against
  OpsBridge in the pilot. The check reports, by collection and key:
  - a file that is missing, too large or doesn't match its digest;
  - a collection the pack doesn't read;
  - a parse or mapping problem;
  - an invoice or purchase order whose number isn't its key;
  - a record listed under a purchase order it isn't for;
  - an invoice whose purchase order isn't in the export.

**Corrected before any pilot (1 November).** The reference file named an OpsBridge row's case
by case number. Intake assigns that number, and a blind decision is recorded before intake.
So the reviewer would have typed the number in afterwards, and a wrong one would have scored
the decision against another case: the report caught only numbers that matched no case. An
OpsBridge row now names its invoice, number and supplier, exactly as intake identifies a
case. Intake allows one case per invoice and supplier in a tenant, so a row finds at most
one case.
- A `manual` row may not carry an invoice number, so its pseudonym rule is now checked.
- The report names cases by number and rows by line, never by invoice.
- The measures did not change, and no pilot data, sample or agreement existed.

**Evidence.**
- `tests/pilot/test_reference.py` (19): strict reading, line-numbered problems, manual
  timings in order, the invoice normalized as intake normalizes it, and a pseudonymous
  manual row. `pilot-check-reference` reads the file the same way without a database, so the
  operator finds a problem while the row is fresh (added 2 November).
- `tests/pilot/test_measures.py` (6): each measure on hand-written facts whose counts can be
  checked by reading them; a row matches a case only when number and supplier both agree.
  Matching on the number alone, or reading the invoice without intake's normalization, each
  makes a test fail.
- `tests/integration/test_pilot_report.py` (2): cases worked through the API (approved as
  drafted, edited then approved, resolved, rejected and excluded), with another tenant's
  case absent. The rows name invoices, one typed in lower case, and the report names none. The report writes nothing. Its transaction refuses a write, and the test fails
  when `SET TRANSACTION READ ONLY` is removed.
- `tests/pilot/test_export_check.py` (9): hand-written IT-pack exports. Every unreadable
  record is listed, not only the first. A consistent export has nothing to review, and its
  report holds no document content. The command's exit codes are tested too. Each test was
  checked against a deliberately broken check: removing the number and listing comparison,
  the purchase-order lookup or the mapping problems, or treating a partial export's missing
  record as absent, each makes a test fail.
- Added 2 November: a problem can quote a document's own value, and `export-check` printed
  it raw. An invoice number holding `ESC [2K` erased its own problem line on the operator's
  terminal, and one holding `ESC ]0;…` retitled the window. The command now spells out
  control and format characters (`\x1b`, `‮`) in what it prints; the JSON report was
  already escaped. Without the fix, the new test fails, and it also fails when only ASCII
  control characters are escaped, which lets a bidirectional override through.

**Not done.** No pilot: there is no export, reviewer or authorization. The report and the
export check have run only on synthetic data. One reviewer gives one person's judgement,
and blindness is shown only by the recorded time.

## Work plan

1. Done locally, each with failing-first regressions and a passing full gate: A01, A02,
   A03, A04, A05, A13, A17; in part A06 (public-only verifiers, registered credentials), A15
   (collection budget), A16 (session pruning); prepared for the owner A12 (ruleset, release
   workflow).
2. Done locally since: A08's first step without access, an export-file source held to the
   `purchasing-source/1` contract with hand-written fixtures (not proof of any real system).
3. Closure pass (29 October), from `70c2e43`:
   - compressed replies count only when complete (A04);
   - the intermittent GitHub timeout test was diagnosed (a fixture timed on another clock,
     not a connector defect) and its fixtures fixed;
   - the finding-text defect is fixed as `investigation/1.2.0`, with v1 replayed under 1.1.0
     (A13);
   - release qualification corrected (A12);
   - both scoring schemes covered in the browser (A03).

4. After the closure review (30 October; PR #17 merged as `4d2f1f6`, whose CI run on `main`,
   41085903247, passed all four jobs and which `release_check.py ci` accepts; this work was
   merged on 1 November as PR #20, `b63c5fc`, whose CI run on `main`, 41277454673, passed
   all four jobs and which `release_check.py ci` accepts):
   - the engineering re-run `opsbridge-bench-v1-rerun` was recorded under scoring 2 and
     `investigation/1.2.0` as regression evidence (A03, A13);
   - replies read by the Anthropic and MCP SDKs are bounded (A04);
   - an experimental pre-release can be tagged and is labelled as one; the packages declare
     `0.1.0-alpha.1` (A12);
   - the single-source pilot's measures are defined and computed by `pilot-report`, tested
     on synthetic cases only (A08, A14);
   - `export-check` reads a pilot export through its pack before the first case, so a defect
     in it is found before it costs a pilot case (A08);
   - before any pilot, reference rows name the invoice rather than the case number, which
     doesn't exist yet when a blind decision is recorded (A08, A14);
   - since then: parser subprocesses get none of the service's secrets (A06; PR #23, merged
     as `16951dc`), `export-check` spells out control characters in what it prints (A08; PR
     #24), a sign-out at the identity provider ends OpsBridge's sessions at once (A16; PR #25,
     merged as `23b2f84`), and a release builds, verifies and attests a reproducible
     evidence bundle (A17; PR #27, merged on 3 November as `77a515b`, whose CI run on `main`,
     41568201604, passed all four jobs and which `release_check.py ci` accepts). After
     that, the compose stack mounts the delegation signing key only in bootstrap and the
     worker (A06; PR #28, merged on 4 November as `87d4665`, whose CI run on `main`,
     41889856619, passed all four jobs and which `release_check.py ci` accepts), and
     parser subprocesses are confined with Landlock, required when deployed (A06; PR #29,
     merged on 5 November as `185c4c7`, whose CI run on `main`, 41942800839, passed all four
     jobs and which `release_check.py ci` accepts). After that, confined parsers can open
     no socket (A06), and `tests/test_workflows.py` checks the workflows before they run:
     it found that the release did not repeat CI's mount-point check, now added (A12; PR
     #30, merged on 5 November as `79992cd`, whose CI run on `main`, 41980269125, passed all
     four jobs and which `release_check.py ci` accepts). The owner then applied the `main`
     ruleset, and a test ties its required checks to CI's job names (A12).

   Remaining limits:
   - the release workflow has not run; its preconditions pass on `79992cd`;
   - there is no held-out or out-of-sample result beyond v1;
   - the SDKs' streaming APIs are unused and unbounded.
5. Needs the owner:
   - push `v0.1.0-alpha.1` on `main`'s head, `79992cd` (PR #30), which carries the fixed
     pypdf and OpenSSL, the HIGH gate and the evidence bundle (A12, A17);
   - a purchasing system or export and authorization, and an AP or procurement reviewer
     for the single-source pilot (A08, A14);
   - a model budget, credential and independent labellers (A09, A10);
   - a cloud account for staging (A11; also per-role secrets, A06, and load figures, A15);
   - operator sessions (A14);
   - a deprovisioning policy (A16).
