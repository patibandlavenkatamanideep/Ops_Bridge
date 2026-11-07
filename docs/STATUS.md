# OpsBridge status

Last updated 2026-11-06. `main` is at `79992cd` (CI run 41980269125 green). Work continues on
`feat/opsbridge-foundation` in PR #36, whose CI passed all four jobs on `ec81177`
(42089061306); later commits there change documentation only.

This page says what is true now. The journal of how it got here, 14 October to 5 November, is
[docs/status-history.md](status-history.md); findings, evidence and blockers per review are in
[docs/production-readiness.md](production-readiness.md).

## Validation levels

| Level | State |
|---|---|
| Local | Built and verified on macOS: unit 733 passed (5 Linux-only skipped), integration 281 on real PostgreSQL, Keycloak and MCP, canary 64/64, 34 receipts verify, every recorded run replays case by case |
| Hosted CI | Passing on `main` and on PR #36's head. CI also runs what this host cannot: the image build and scan, the parser sandbox (Landlock and seccomp), OCR, and the browser tests |
| Real model | Not evaluated: no credential, budget or labellers; the deterministic mock only |
| Task tracker | Contract-tested against a fake of GitHub's API; no live write |
| Purchasing source | The simulator and hand-written exports only; no customer system or export has been read |
| Staging | Not provisioned. The runtime depends on a parser-sandbox check on AWS (docs/deployment.md) |
| Pilot | Not started. Protocol `opsbridge-pilot/2` is prepared and tested on synthetic data |
| Release | None published. `v0.1.0-alpha.1` qualifies on `79992cd`; pushing the tag is the owner's |

## Open work in the repository

- **PR #36**, waiting for the owner to merge. It fixes the review of 5 November (N01–N08):
  strict JSON, request bodies by received bytes, source clocks, bounded evidence bundles, the
  historical pilot report, credential ownership, truthful deadlines and offline egress. It
  also adds migrations 0023 and 0024, narrows the release's attestation check and prepares
  release-tag rulesets.
- **Dependabot:** eight pull requests, all behind `main`. The ruleset requires a branch up to
  date before it merges ("Update branch"). The Python base-image bump (#31) should be rechecked
  after #36: the image now pins `apt` 2.6.2, and a newer base image may already carry it.

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

- Name resolution is not covered by the outbound deadline; a silent resolver delays by its
  own timeout. The offline egress guard covers Python sockets, not native code (libpq).
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
  New runs use `investigation/1.3.0`, which lists the stored reason on every read; the
  recordings keep their versions and order, and an out-of-order replay fails closed.
- The S3 store is tested against SeaweedFS only, and alerts reach a local sink only.
- Processes need synchronized clocks. Comparisons with recorded times use the database's
  clock, and so does the pilot report's cut-off.

## Next executable task

1. Owner: merge PR #36. Then fast-forward the branch, check `main`'s CI and the release
   preconditions, and name the merge commit for the tag in README, this page and the readiness
   record (a test keeps those three in agreement).
2. Owner: the pilot's inputs above. Then run `export-check` on the export, freeze the cohort,
   and start the protocol.
3. Owner: a cloud account. Then run `parser-sandbox-check` in one Fargate task first.
4. Locally, while waiting: nothing in the brief can advance without these inputs except
   preparation already done. New findings get a regression test first, as before.

## Recent entries

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
