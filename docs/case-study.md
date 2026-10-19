# Engineering case study: defects that changed the implementation

Real defects found while building OpsBridge, how they were found, what changed, and how to
reproduce the evidence. All on synthetic data: sections 1–5 on the development machine
(2026-10-14/15), section 6 in the first hosted CI runs (2026-10-17).
Commit ids are on the `feat/opsbridge-foundation` branch.

## 1. A "safe" retry that created a duplicate task (Phase 9)

**Found by:** a browser test written to check the demo script, not by the integration suite.
The demo step "turn off the task tracker's idempotency and search, then cause a timeout
after commit" was expected to end in *outcome unknown — needs review*. It ended *confirmed*.

**Evidence:** the operation's attempt history read
`dispatch: ambiguous_failure → reconcile: lookup_unavailable (×2) → dispatch: succeeded →
readback: succeeded`, and searching the tracker for the operation marker returned **two**
tasks for one approved operation.

**Cause:** the simulator connector hard-coded `idempotency_keys=True`. The executor's rule
was right — after an ambiguous failure with no conclusive lookup, resend only if idempotency
keys make it safe — but it trusted a declaration the remote no longer honoured. The
integration tests had always changed the connector's declared capabilities together with the
simulator's behaviour, so they never covered a declaration that disagrees with reality.

**Change (49e0cba, b7eb105, 955ca3c):** the tracker advertises its current capabilities; the
connector discovers them for each dispatch (worker and Casework MCP service agree), and if
discovery fails it assumes the weakest contract — no idempotency, no lookup — so an ambiguous
result goes to a person instead of being resent. The rule in the documentation became
explicit: capabilities are a contract that must be validated against the real provider.

**Reproduce:** `pytest tests/integration/test_approvals_execution.py -k stops_honouring` and
`tests/integration/test_mcp.py -k unreliable_remote` (both fail on the old connector: status
*confirmed*, two tasks); `frontend/e2e/demo.spec.ts` step 4b in the browser.

**Lesson:** a mock that changes its declaration and behaviour together tests the
executor's logic, not the system's assumptions. The benchmark's "no duplicate effects" gate
held for its declared trial set and still missed this.

## 2. The worker died when the database restarted (Phase 7 → re-verified in Phase 8)

**Found by:** rebuilding the PostgreSQL image (`make infra-up --build`) while the dev stack
ran. The worker raised `OperationalError: the database system is shutting down` out of its
loop and exited; the dev supervisor then stopped everything.

**Change (0a6f338):** the loop catches database errors and backs off (0.5 s doubling to
30 s) instead of exiting; leases and fencing tokens already made the resumed claim safe.

**Evidence after the fix:** `tests/workflow/test_worker_resilience.py`; in the container
recovery drill, restarting PostgreSQL under the running stack logged one
`database unavailable … retrying in 0.5s` and the worker continued without a restart
(docs/operations.md, "Drills").

## 3. An alert that could never fire (Phase 8)

`opsbridge_artifact_fs_bytes{state="free"} / opsbridge_artifact_fs_bytes{state="total"}`
matches no series, because the two sides differ in the `state` label. The rule loaded
cleanly and would have stayed silent while the disk filled. **Found by** a `promtool` unit
test with synthetic series; **fixed** with `ignoring(state)` (b8f532a). Reproduce:
`make alerts-test`. The same alert later went *pending* for real on the development laptop
(10% free), which the drill recorded as a host condition, not an OpsBridge one.

## 4. The framework trusted trace context from the internet (Phase 8)

FastAPI 0.143 creates its own server spans when a tracer provider exists. A test asserting
that the request span carried the tenant id failed; investigation showed FastAPI's span was
the real parent, that it **extracted any incoming `traceparent`** and recorded raw URLs and
queries. **Changed (51f668f):** native telemetry disabled through its documented option; the
perimeter middleware is the only server span, starts a new trace per request and labels by
route template. Test: `test_incoming_trace_context_is_not_trusted`.

## 5. Smaller defects, same pattern: found by a check, fixed at the cause

| Defect | Found by | Fix |
|---|---|---|
| Simulator route ordering captured `/orgs/{org}/tasks/{id}` as a record route (Phase 4) | the executor's read-back refused to confirm a task it could not read back | task routes registered first; read-back stayed mandatory |
| OIDC callback `code`/`state` printed in access logs (Phase 6) | reading the dev log output | redacting filter on every handler incl. uvicorn access log; test applies uvicorn's real config |
| Chunked request bodies bypassed the size limit (Phase 6) | threat review of the perimeter | 411 for chunked writes; test |
| Findings stated numbers missing from their own values, so faithful model text failed validation (Phase 7) | validation benchmark: 12 rejected model outputs | values completed (rules 1.0.1) + invariant test over dev and validation corpora |
| Podman re-split health-check arguments (`python -c "…"` became `import`) (Phase 8) | clean-launch drill: simulator never healthy | `opsbridge probe` command with no spaces in arguments |
| MCP SDK rejected container hostnames (DNS-rebinding default) (Phase 8) | container profile design review | explicit allowed hosts from each service URL; protection kept on |
| The frozen benchmark plan stored the scoring definitions but `verify` never compared them with the code, so a grading change would have run silently and looked comparable | mapping each acceptance scenario of the brief to the test that checks it | `verify` compares scoring; the evaluation view flags runs from another plan or grader as not comparable |
| The upload confirmation disappeared after the first upload into a case with no documents: the form moved in the component tree and was remounted (after hosted CI) | the new OCR browser test, run on a fresh case | the uploader keeps one position in the tree |
| Evidence table squeezed into unreadable columns at 1280 px (Phase 9) | inspecting the real UI for README screenshots | document list moves above the panels below 1440 px |

## 6. What the first hosted CI runs found (2026-10-17)

Until then every gate had run on one Mac with a long-lived development database and
simulator file. The first two runs on GitHub's ubuntu-24.04 runners failed (PR #1, runs
38999032200 and 38999754931); every failure was a real defect, not a CI quirk.

| Defect | Found by | Fix |
|---|---|---|
| A stale ruff cache passed an import-order error locally: `src = ["src", "tests"]` plus the `tests/mcp` package made ruff treat the MCP SDK as first-party | CI lint; reproduced with `ruff check --no-cache` | `mcp` declared third-party; `make lint` and `verify.py` run ruff without its cache (54c1ac0, 6302cf7, 77dd22b) |
| On a fresh machine `make dev` started an empty simulator, so every investigation ended in needs-information; the README quickstart had the same gap, hidden locally by a simulator file seeded on the first day | CI browser tests: 8 of 23 failed; the simulator answered 404 for every demo record | the dev stack upserts the dev corpus before starting the simulator, as the simulator container already did (dae7088) |
| Browser tests depended on leftover data: the a11y and demo specs opened an `INV-9001` case that only a later spec creates, and a timed-out demo test left an injected simulator fault that broke the next tests | CI browser tests: 4 of 23 still failed; reproduced on a fresh local database, where the old a11y test failed identically | tests create their own case; faults are cleared before each demo test and after the file (e666d5c–1f29197) |
| Transport failures in attempt records read only "ExceptionGroup: unhandled errors in a TaskGroup (2 sub-exceptions)" | reading those records while diagnosing the row above | attempt records name the leaf errors (df52112) |

**Evidence after the fixes:** run 39000811947 at 1f29197 passed all three jobs (unit 253,
integration 123, browser 23); locally the browser suite passed 23/23 twice on a fresh
database and once on the long-lived one.

**Lesson:** the clean-clone check had run only the offline gates, and the clean container
launch seeds its own simulator, so nothing had started the host development stack from an
empty state. A fresh environment is a test input; so is test order.

## What did not change

The decision rules stayed deterministic, approvals stayed bound to the reviewed version,
and no test was weakened to pass. A vacuous unbounded-retries test was removed, and a
retrieval-isolation test whose query matched nothing (so it passed trivially) was rewritten
so that it can fail.
