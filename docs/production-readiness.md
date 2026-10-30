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
| A06 | Verifiers load the private signing key; credential references not tenant-owned | Partly fixed, verified locally: public-only verifiers with key rotation, tenant-registered credentials. Not done: per-role database and secret access, parser sandbox (staging work) | 3 | See "Package 3, keys and credentials" below | Staging (A11) for per-role secrets |
| A07 | Important fixes only in open PR #17 | This branch contains PR #17; the merge is the owner's | 0 | PR #17 green in CI at `732e84d` | Owner merges #17 |
| A08 | No actual purchasing source | Blocked for live proof. Done locally: an export-file source held to the contract (hand-written exports only) | 5 | See "Package 5, export source" below | Owner chooses a system or export, provides a real (sanitized) export or sandbox, and authorizes access |
| A09 | AI usefulness not measured on its actual task | Blocked for real-model evidence. Protocol prepared (`docs/evaluation-plan-v2.md`: measures, model identity, failures in denominators) | 6 | Mock-only results; no paid calls made | Model credential, budget and authorization |
| A10 | Labels by the same author; held-out set public | Blocked. Protocol prepared (`docs/evaluation-plan-v2.md`: independent labelling, adjudication records, family-sequestered split, sample sizes chosen in advance) | 6, 9 | — | Independent labellers and new data |
| A11 | Staging is a plan only | Blocked | 8 | `docs/deployment.md` plan | Cloud account, choice and spending authorization |
| A12 | `main` unprotected; no release promotion | Prepared, not applied: ruleset file and release workflow (tag equals every declared version; CI evidence read from the Actions API; the digest published is the digest qualified; SBOM, provenance attestation, verification) | 8 | See "Package 8, governance and releases" below | Owner applies the ruleset and pushes the first tag |
| A13 | Replay compares next actions only; mock replay depends on rules-only trials running first | Fixed, verified locally: behavioural replay comparison; the cause (a finding-text defect) fixed as workflow `investigation/1.2.0`; v1 recordings replay with `investigation/1.1.0` in their declared order, and other orders fail closed | 4 | See "Package 4, behavioural replay" below | — |
| A14 | Operator workflow unvalidated | Partly: PR #17 ownership, views, settings sections, keyboard focus | 7, 9 | Browser tests | Operator sessions |
| A15 | Capacity and spend bounds unmeasured | Partly: one time budget for reading the source per investigation. Load figures, SLOs and spend accounting need staging | 3, 8 | See "Package 3, collection budget" below | Staging and the owner's expected load |
| A16 | Identity and privacy lifecycle incomplete | Partly: ended sessions and their ID tokens are now pruned. Profile deletion, identity-provider disable propagation, backups and object versions are not done | 3, 8 | See "Package 3, session lifecycle" below | The owner's deprovisioning policy |
| A17 | Image lacks the evaluation evidence the repository verifies | Decided and tested locally: summary-only in the image, full verification from the repository; image check in CI | 8 | See "Package 8, evidence packaging" below | — |
| A18 | Large modules; status documents drift | Ongoing: README status refreshed 2026-10-28 against this record; each package updated STATUS, this record and the runbooks. Shared contracts extracted where they removed duplication (`domain/validation.py`, `services/execution_admission.py`, `connectors/http_transport.py`); no broad refactor | all | — | — |

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

**Prepared** (nothing applied; repository administration and publishing are the owner's):
- `infra/github/main-ruleset.json`: pull requests only, the four CI jobs required on the
  latest commit, review threads resolved, no force push or deletion; administrators may
  bypass only through a pull request (break-glass). Required approvals 0 while there is one
  maintainer. The check names were matched against the CI job names. The apply command is in
  docs/deployment.md.
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
    partial exit, API health, SBOM, and fixable critical vulnerabilities;
  - it attests and verifies the digest;
  - only then does it give the same digest the version tag, without rebuilding, refusing if
    the tag resolves to anything else.

  Actions are pinned to commits of releases at least two weeks old.

**Evidence**: `tests/test_release_check.py` has 14 tests, covering:
- a qualifying run;
- refused look-alikes: another commit, a pull-request event, another branch, a workflow file
  with the same job names, a fork, another repository, a failed or unfinished run;
- a failed, missing or other-run job;
- malformed and mismatched tags;
- every version declaration read, and one left behind by a bump caught;
- job names that match `ci.yml`.

Run read-only against this repository, `main`'s head `468dae7` qualifies and the PR head,
never pushed to `main`, is refused. The five declarations agree on 0.1.0. The ruleset's check
names match CI. The workflow has not run (no tag), so its first use is its first test, and no
attestation exists yet. The promotion relies on `docker buildx imagetools create` keeping a
single image index as it is. The step therefore refuses, before tagging, a candidate that is
not an index; the build's provenance attestation makes it one. It checks again after tagging
that the tag resolves to the qualified digest. The candidate tag stays in the registry.

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

**Not done**: deleting user profiles when an account leaves; propagating an identity-provider
disable to sessions that are still valid (sessions are checked locally up to their idle and
absolute limits, 30 minutes and 8 hours); backups and object versions. These need the owner's
deprovisioning policy.

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

4. After the closure review (30 October; local branch `feat/after-pr17`, kept off PR #17
   until it is merged):
   - the engineering re-run `opsbridge-bench-v1-rerun` was recorded under scoring 2 and
     `investigation/1.2.0` as regression evidence (A03, A13);
   - replies read by the Anthropic and MCP SDKs are bounded (A04).

   Remaining limits:
   - the release workflow has not run, and can't pass until this code is on `main`;
   - there is no held-out or out-of-sample result beyond v1;
   - the SDKs' streaming APIs are unused and unbounded.
5. Needs the owner:
   - merge PR #17 (A07), apply the `main` ruleset and push the first release tag (A12);
   - a purchasing system or export and authorization (A08);
   - a model budget, credential and independent labellers (A09, A10);
   - a cloud account for staging (A11; also per-role secrets, A06, and load figures, A15);
   - operator sessions (A14);
   - a deprovisioning policy (A16).
