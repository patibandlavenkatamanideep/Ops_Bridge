# OpsBridge status

Last updated 2026-11-17. `main` is at `53e6c5a`, PR #45's merge (CI run 44078427478 green on
the first attempt). This record's update is PR #46.

This page says what is true now. The journal of how it got here, 14 October to 5 November, is
[docs/status-history.md](status-history.md); findings, evidence and blockers per review are in
[docs/production-readiness.md](production-readiness.md).

## Validation levels

| Level | State |
|---|---|
| Local | Built and verified on macOS: unit 927 passed (5 Linux-only skipped, and the live tests), integration 325 on real PostgreSQL, Keycloak and MCP, canary 64/64, 34 receipts verify, every recorded run replays case by case |
| Hosted CI | Passing on `main`. CI also runs what this host cannot: the image build and scan, the parser sandbox (Landlock and seccomp), OCR, and the browser tests |
| Real model | Not evaluated: no credential, budget or labellers; the deterministic mock only |
| Task tracker | Contract-tested against a fake of GitHub's API; no live write |
| Purchasing source | The simulator and hand-written exports only; no customer system or export has been read |
| Receiving and disposition | Implemented and tested on synthetic data (rules `opsbridge-rules/1.4.0`, docs/receiving-disposition.md): delivered, accepted and rejected kept apart; late rejections, returns, replacements and credits; a stale approval refused at both write boundaries. No organization's receiving data read; no reviewer has judged the questions or owners; no shipped policy names a disposition owner (a labelled synthetic version does, for tests only) |
| Staging | Not provisioned. The runtime depends on a parser-sandbox check on AWS (docs/deployment.md) |
| Pilot | Not started. Prepared and checked on synthetic data: protocol `opsbridge-pilot/2`, a per-case export check, and a rules-only dry run with outbound writes disabled (docs/pilot/quickstart.md). The external first pass is blocked: no authorized export, reviewer or authorization |
| Release | None published. `v0.1.0-alpha.1` qualifies on `53e6c5a` (`release_check.py versions` and `ci` pass); pushing the tag is the owner's |

## Open work in the repository

- **PR #36** (the review of 5 November, N01–N08), **PR #37** (name lookups within the
  outbound deadline; proposal status changes and attempt leases on the database's clock) and
  **PR #38** (the approval-wait and execution-time metrics measured between two database
  times; the clock claims corrected) are merged, and so is **PR #39** (the image on Python
  3.12.16 without the OpenSSL and `apt` pins; action updates held 14 days) and **PR #45**
  (OpenTelemetry's API, SDK and OTLP exporter raised together to 1.45.2).
- **PR #47** (draft): the pilot's preparation. A rules-only dry run with outbound writes
  disabled, a per-case preflight, scenario A fixed (rules 1.3.0), scenario B checked, and the
  36/40 question answered case by case. Handoff in docs/pilot/.
- **PR #48** (draft, receiving and disposition; its branch starts at PR #47's head): receiving events and rejection reasons (migration 0025), rules 1.4.0 and
  workflow 1.5.0, the Receiving tab, and the synthetic receiving pack. It changes the
  rehearsal's scenario A (now asks receiving for the inspection record), so it follows #47.
- **PR #49** (draft, source freshness at dispatch; its branch starts at PR #48's head): before
  a follow-up task is sent, the executor asks the purchasing system whether it still holds
  what was approved. If it changed or cannot say, nothing is sent and the action returns to
  review. With an export, follow-up tasks stay in review.
- **PR #52** (draft, pilot tenant provisioning; its branch starts at PR #49's head):
  `tenant-provision` creates one organization's tenant, never a demo tenant, from a spec file,
  in one audited transaction. Members join by invitation in the app.
- **Dependabot:** seven pull requests. #43 (actions, both now past two weeks) and #44 (npm)
  passed CI. #42 (14 Python updates, among them `anthropic` 2.1.0 to 2.5.0 and `fastapi`
  0.143.1 to 0.162.0) passed every test; its image job failed on the same download as
  `main`'s and was re-run. #32 moves the UI build stage to Node 26, a major version; #12, #10
  and #9 are older frontend majors. The ruleset requires a branch up to date before it merges
  ("Update branch"). Dependabot closed #31 itself on 7 November ("up-to-date now").

## Blockers (each affects only the work that depends on it)

| Needs | Blocks |
|---|---|
| An authorized purchasing export or system, an AP or procurement reviewer, and a frozen cohort | The single-source pilot (A08, A14; docs/pilot-plan.md) |
| A cloud account, a spend ceiling and an identity provider | Staging (A11), whose first step is one task running `opsbridge parser-sandbox-check` |
| A model credential, a budget and independent labellers | Evaluation v2 (A09, A10; docs/evaluation-plan-v2.md) |
| Operator sessions | Usability and effort measures (A14) |
| Decisions on deprovisioning | The account lifecycle (A16; docs/account-lifecycle.md lists six) |
| Authorization to push `v0.1.0-alpha.1`, and to apply the release-tag rulesets | The first release (A12) |
| A GitHub sandbox repository, a token and authorization for writes | A live task-tracker run |

## Known limitations

- A name lookup that never answers no longer delays a request, but keeps its thread until the
  system resolver gives up (16 at most). The offline egress guard covers Python sockets, not
  native code (libpq).
- Rate limits are shared across API instances but are no DDoS defence; the edge's own body and
  time limits are set at staging.
- PDF and e-mail extraction rely on pack-specific patterns: unseen layouts go to review. OCR
  was measured on one clean raster only. Retrieval is lexical full-text search.
- Uploads are not malware-scanned. Nothing deletes or anonymizes a profile, and there is no
  legal hold. Backups and object versions outlive retention until they expire.
- The benchmark's labels were written by the same agent that wrote the rules, and its responses
  are mocked: it is engineering evidence, not an accuracy claim.
- The v1 recordings depend on their run order: under the workflow versions they were made
  with (1.1.0, 1.2.0), a document's read problems were listed only on its first read in a run.
  New runs (`investigation/1.3.0` and later) list the stored reason on every read; the
  recordings keep their versions and order, and an out-of-order replay fails closed.
- Evidence that changes after approval stops the dispatch. The Receiving tab lists the
  documents added or no longer current since each approval and a timeline of receiving
  evidence, but there is no field-by-field diff, and the refusal's reasons are in the audit
  log, not the API (the UI shows its category).
- A pilot tenant is provisioned with `tenant-provision` from a spec file. It has been run only
  against the local database and synthetic specs, never against an organization's identity
  provider.
- Receiving: any evidence change, material or not, makes an approval stale. (A rejection
  only in the purchasing system now stops the dispatch: the executor asks the source first.
  With an export it cannot, so external writes stay in review.) No
  policy names a disposition owner or a hold scope, so the demo tenant sends rejected goods
  to manual review. Only the furniture pack maps receiving events, and the agent's tools do
  not see them (docs/receiving-disposition.md, section 11).
- The S3 store is tested against SeaweedFS only, and alerts reach a local sink only.
- Processes need synchronized clocks. Each comparison takes both its times from one clock: the
  database's for recorded events, leases, the pilot cut-off and the duration metrics; the
  application's for the expiry times it sets (docs/security-and-threat-model.md, T45).

## Next executable task

1. Owner: push `v0.1.0-alpha.1` on `main`'s head when ready (`53e6c5a` qualifies now).
2. Owner: the pilot's inputs above: an authorized, sanitized export, one AP or procurement
   reviewer and written authorization. Then follow docs/pilot/quickstart.md, section 6
   (pre-registration first).
3. Owner: a cloud account. Then run `parser-sandbox-check` in one Fargate task first.
4. Owner, for receiving: who resolves rejected goods with the supplier (a `disposition`
   route) and whether a disputed quantity holds the whole invoice or only the disputed
   portion. Each is published as a new policy version; until then the split is shown and the
   payment recommendation left unresolved.
5. Locally, while waiting: nothing in the brief can advance without these inputs except
   preparation already done. New findings get a regression test first, as before.

## Recent entries

### 2026-11-19: pilot tenant provisioning (PR #52, draft, after #49)
- Before: only the synthetic demo tenants could be created (`seed-demo`), and their accounts
  share one password. The quickstart left a pilot tenant to the owner, with no command.
- `tenant-provision --spec FILE [--dry-run]` (`provision.py`, template
  `fixtures/pilot/tenant.example.yaml`) creates one organization's tenant in one audited
  transaction:
  - the tenant, not flagged as a demo;
  - settings, rules only by default;
  - the first task connection;
  - its policy versions, validated for the pack;
  - its first administrators, by identity-provider subject.
- Everyone else joins by invitation in the app. A re-run publishes added policy versions and
  changes nothing else.
- Refused, with nothing written: a demo slug or tenant, a demo account's subject, another
  pack, a policy for another pack, a changed policy under the same version, a model mode the
  tier lacks, and unfilled template values.
- Tests: 23 unit (the spec), 7 on the stack:
  - what is written and audited;
  - the administrator invites through the app, and a demo administrator has no access;
  - re-runs, atomic refusals and dry runs;
  - `seed-demo` leaves the tenant alone.
  - A mutation flagging the tenant as a demo fails 5 of them.
- Unit 927 (frontend 17), integration 325.

### 2026-11-18: source freshness at dispatch (PR #49, draft, after #48)
- Reproduced on #48's head: a follow-up approved on 20 accepted was sent after a rejection of
  2 was recorded in the purchasing system only (case not re-run). Local freshness could not
  see it, and the 24-hour window was open.
- Before every new external write the executor now asks the purchasing system whether it
  still holds what the approval rests on (`workflow/source_check.py`). Changed: the operation
  is `source_changed`. Cannot tell (an export, an outage, an incomplete listing): it is
  `source_unverified`. Either way the connector is never called and the proposal is
  superseded.
- Tests: 6 on the stack, 14 unit. The regression test fails with the check switched off.
  Unit 904 (frontend 17), integration 318, canary 64/64, every recording replays.
- Exports cannot establish freshness, so with an export follow-up tasks stay in review (the
  pilot keeps outbound writes disabled anyway).

### 2026-11-18: receiving and disposition (PR #48, draft, after #47)
- Baseline on `344225d`: a short delivery and a rejection with the same 18 accepted asked
  receiving the same question. A rejection recorded after the receipt was ignored (the case
  matched). A return of rejected units subtracted them twice. The rejection reason was
  dropped, and a receipt with no status was read as accepted. 26 new rule tests failed
  there; the 4 controls passed.
- Rules `opsbridge-rules/1.4.0`, workflow `investigation/1.5.0`, migration 0025
  (`receiving_events`, `receipt_lines.reason`): receiving events with their own occurrence
  time; disposition questions with an owner from policy, or a statement that none is set;
  the matched and disputed split, with no amount and no payment action.
- After approval: a late rejection stops the pending follow-up at the worker and the casework
  tool; an identical reimport keeps the approval. After execution the action is kept, an audit
  event records the change, and the Receiving tab asks for reconciliation.
- Every frozen recording replays case by case. The canary changed only its rules version and
  a `hold_unconfigured` note on 18 cases. The rehearsal's scenario A was relabelled
  (inspection record), with the reason in the fixture.
- Unit 885 (frontend 17), integration 311, canary 64/64, 33 receipts verify, every recording
  replays case by case. The two 18-unit cases and the late-rejection demo were walked in a
  browser against `make dev`.

### 2026-11-17: pilot preparation (PR #47, draft)
- Scenario A: 20 billed, 18 accepted, 2 rejected, and the supplier says all 20 arrived. The
  finding said "only 18 were received", cited no rejected row, and called the supplier's
  statement a conflict. Rules `opsbridge-rules/1.3.0` count and cite the rejected units
  and note that the claim agrees with receiving. New runs use `investigation/1.4.0`;
  recorded runs keep rules 1.2.0 and replay case by case.
- Scenario B passes on the existing checks: a follow-up approved on 8 of 10 is not sent
  once a corrected receipt is read, and the case returns for review.
- `OPSBRIDGE_OUTBOUND_WRITES=disabled` refuses writes outside OpsBridge in the worker and
  Casework MCP, with its own category so approvals stand. `/readyz` reports it.
- `export-check` says per invoice whether a case can be evaluated reliably;
  `pilot-rehearsal-export` writes five synthetic cases as an export.
- 36/40: both modes missed the same four held-out cases in every repeat; no discordant case
  (`scripts/bench_pairing.py`).
- Unit 770, integration 295, canary 64/64, replay check reproduces every recording; the
  quickstart passed from a clean clone, with a browser walk-through.

### 2026-11-17: PR #45 merged and qualified
- PR #45 merged as `53e6c5a`. Its push CI run on `main`, 44078427478, passed all four jobs on
  the first attempt, and `release_check.py versions` and `ci` pass on that SHA (checked in a
  clean worktree of it). README, this page and the readiness record name it.

### 2026-11-16: PR #39 merged; OpenTelemetry raised by hand (PR #45)
- PR #39 merged on 8 November as `2cd06dc`. `main`'s CI failed there: GitHub answered 504 to
  the SBOM step's download of Syft's checksums, after every test had passed. The failed job
  was re-run and passed (42468377115, attempt 2); `release_check.py versions` and `ci` pass
  on `2cd06dc`. Just after the re-run, `ci` still failed, because the Actions API's `status`
  filter showed the run in progress; about four minutes later it passed. That was lag, so
  nothing changed.
- Dependabot's weekly `uv` update failed on OpenTelemetry's API, SDK and OTLP exporter, which
  pin one another's version. All three were raised together to 1.45.2; only the eight
  OpenTelemetry packages change in the lock. ADR 0002 says to raise them together by hand.
- Unit 754 passed, integration 288. The first integration run after the Podman VM booted had
  one failure (`test_machine_identity_token_is_audience_scoped`); its message was not kept.
  It passed alone, in a second full run, and straight after a cold Keycloak restart.

### 2026-11-07: PR #38 merged; the Python 3.12.16 base; actions held two weeks (PR #39)
- PR #38 merged as `4615569`; `main`'s CI passed on it (42356139646), and `release_check.py
  versions` and `ci` pass on it. README, this page and the readiness record name it.
- Dependabot #31 checked by reading each base image's package database from the registry.
  The tag's build of 5 November carries OpenSSL 3.0.22-1~deb12u2 and `apt` 2.6.2; #31's build
  of 28 October still had `apt` 2.6.1. The image now uses the newer build without the two
  pins, and the alert sink, ADR 0002 and the deployment guide name 3.12.16. The image job
  passed on it, the HIGH gate included (42358423116).
- Dependabot waited only its default 3 days before proposing an action (#33: `setup-uv`,
  ten days old). Its action updates now wait 14 days, the record's rule; the new test in
  `tests/test_workflows.py` failed first.
- Unit 754 passed.

### 2026-11-07: PR #37 merged; durations and clock claims (PR #38)
- PR #37 merged as `ff24e06`; `main`'s CI passed on it (42340729942), and `release_check.py
  versions` and `ci` pass on it. README, this page and the readiness record name it.
- The approval-wait and execution-time metrics subtracted a database time from the
  application's clock: about 300 s with it 5 minutes ahead, 0 with it behind. They subtract
  two database times now (`tests/integration/test_observability.py`).
- #37's threat-model entry T45 said every row is dated by the database; many are dated by the
  application. Corrected after checking each comparison: each takes both its times from one
  clock, and the session prune, which mixes them, waits 24 hours by default.
- Unit 753 passed, integration 288.

### 2026-11-06: PR #36 merged; lookups and recorded times (PR #37)
- PR #36 merged as `e0f1e2d`; `main`'s CI passed on it (42336948253), and `release_check.py
  versions` and `ci` pass on it. README, this page and the readiness record name it.
- The outbound deadline now covers the name lookup and each address tried. Before, a lookup
  that answered after 3 s held a 0.5 s request for 3 s, and three unanswered addresses took
  1.6 s against 0.6 s.
- A proposal's status changes were dated by the application's clock, every other recorded time
  by the database's: the pilot cut-off test failed by chance with the database 0.2 s ahead,
  and every time with the application 5 minutes off either way. The in-flight check for
  another worker's attempt mixed the two clocks too: an hour ahead, a worker reconciled and
  dispatched instead of backing off. Both use the database's clock now; the back-off had no
  test before.
- Unit 753 passed, integration 286, canary 64/64, receipts and replays as before.

### 2026-11-06: evaluation v2's labelling prepared before any label (A10)
- `opsbridge eval-labels` reads labels and adjudications strictly. It reports Cohen's kappa
  per field and labeller pair, keeps every disagreement with every label, and builds the
  reference only from independent, reasoned adjudications. The reference is incomplete while a
  dispute remains (exit 1).
- Tested against kappas worked out by hand (`tests/evaluation/test_labeling.py`); dropping
  the chance correction fails two tests. No labeller or label exists.
- docs/developer-commands.md still described `pilot-report` under protocol `/1` (missed in the
  N05 docs). It now names `/2`, `--cohort`, the required `--as-of` and the exit codes.

### 2026-11-06: the run-order defect fixed for new runs (`investigation/1.3.0`)
- Reproduced first: two investigations of an unreadable invoice with unchanged bytes listed
  different uncertainty; the second had lost the invoice's reason. Under 1.3.0 a record's
  problems are the reason stored with the version read, the same for new and unchanged
  bytes; a rejected parse's reason is now listed too.
- 1.1.0 and 1.2.0 stay so the v1 and v1-rerun recordings replay with the versions they pin;
  the replay check still reproduces every recorded run, and the canary is 64/64.
- Unit 735 passed (5 skipped), integration 282 passed.

### 2026-11-06: the status split; the sandbox runtime
- The journal moved to docs/status-history.md unchanged, and this page was rewritten to the
  present (review G: status drift).
- The staging runtime for the parser sandbox was checked against AWS's documentation; nothing
  ran on AWS. Fargate publishes no kernel or security-module details, so one task running
  `parser-sandbox-check` decides. If it fails, use ECS on EC2 with Amazon Linux 2023 on kernel
  6.12 or 6.18: Landlock is enabled in those builds from August 2025, while 6.1 lacks the TCP
  rules. `best_effort` is an owner's decision, never a fallback (docs/deployment.md).
