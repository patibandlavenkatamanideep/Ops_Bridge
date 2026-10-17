# Evaluation results — benchmark `opsbridge-bench-v1`

Plan: docs/evaluation-plan.md (frozen in commit 31515f0, before any held-out run).
Results files: `evals/results/opsbridge-bench-v1/*.json`. Receipts:
`evals/ledger/opsbridge-bench.jsonl` (+ `.sig.jsonl`), public key
`evals/keys/opsbridge-eval.pub`. Re-check with `make eval-offline` or
`uv --directory backend run python -m opsbridge.cli eval-verify`.

**Scope of every number below:** synthetic cases; responses from the deterministic mock
model (labelled `mocked`); the local machine. This is engineering verification of the
agent-assisted path. **Real-model quality was not evaluated** (no credentials, opt-in or
local model). No operator or pilot data exists.

## Held-out comparison (final, run once)

Run `heldout-record-20261015T022744Z`, code commit a98f669 (clean tree), 40 cases × 3
repeats × 2 modes; unit = case, majority over repeats.

| Metric | Rules only | Agent-assisted (mock) |
|---|---|---|
| Correct next action | 36/40 = 0.90 (Wilson 95% 0.77–0.96) | 36/40 = 0.90 (0.77–0.96) |
| Correct outcome / discrepancy types / whole case | 36 / 37 / 36 of 40 | 36 / 37 / 36 of 40 |
| Misses: safe (escalated to a person) / unsafe | 4 / 0 | 4 / 0 |
| Trial errors | 0 of 120 | 0 of 120 |
| Instability (non-majority verdicts / repeats) | 0 cases with any flip | 0 cases with any flip |
| Citations valid | 204/204 | 204/204 |
| Model calls (rejected by validators) | 0 | 243 (0) |
| Explanation origin | template ×120 | model:mock ×120 |
| Median / p95 run latency | 103 / 131 ms | 110.5 / 131 ms |
| Cost | 0 | 0 (mock) |

Per discrepancy type (majority verdicts, both modes identical): quantity mismatch
precision 1.0, recall 8/9; price mismatch precision 1.0, recall 7/8; missing receipt
precision 1.0, recall 1/2 (denominators are small). By expected outcome, correct next
action: matched 15/16, discrepancies found 13/15, needs information 1/2, manual review 7/7.

**All four misses are one family**, `FUR-H-LAYOUT-V2` (fur-heldout-218…221): invoices in a
supplier layout that no pack mapping was written for. OpsBridge could not read them and
routed every one to manual review (`invoice_extraction_needs_review`) instead of the
correct route (quantity follow-up, review note, supplier price query, information request).
These are safe misses — a person reviews — but they are misses: the pack mapping does not
generalize to an unseen template. Every other untuned variation in the held-out set was
handled correctly: effective-dated policy (invoices dated under the superseded v1 policy),
invoices against cancelled POs, rejected receipt lines, billing beyond the ordered quantity,
unit aliases and missing conversions, pack-unit price normalization, PO line discounts,
multi-receipt sequences with returns and reversals, cancelled prior allocations, an
unsupported currency, and a similar-but-different supplier name.

**Paired comparison:** 36 both correct, 4 neither, 0 discordant cases. The benchmark cannot
distinguish the two modes; by design the next action is rule-selected and model proposals
must agree with it, so agent assistance did not change any decision and did not introduce
errors, invalid citations or rejected outputs on this split.

### Acceptance (evals/acceptance-policy.json, frozen before the run)

| Gate | Status | Evidence |
|---|---|---|
| Held-out next action ≥ 0.90 (rules only) | **pass (interval includes values below the target)** — 0.90 exactly; one more miss would fail | this run |
| Held-out next action ≥ 0.90 (mock) | **pass (interval includes values below the target)** — same | this run |
| Security/recovery invariants | pass | `make test-integration` 101 passed (Phase 7 run) |
| No unauthorized writes in the declared trial set | pass (0 observed) | benchmark runs create no operations; MCP/execution denial tests pass |
| No duplicate external effects in the declared trial set | pass (0 observed) | test_approvals_execution.py, test_github_execution.py |
| Real-model quality | **not evaluated** | no real-model dataset |

**Found after the benchmark (2026-10-15, Phase 9):** a demo browser test produced a
duplicate external effect in a configuration outside the declared trial set — the task
tracker stopped honouring idempotency keys while its search was unavailable, but the
simulator connector still declared idempotency support, so a resend after a timeout created
a second task. The "no duplicate external effects" gate holds for the declared trial set it
names, but the defect shows that set did not cover a connector whose behaviour diverges from
its declaration. Fixed by capability discovery (weakest contract when unknown) with
regression tests in-process and through Casework MCP (docs/execution-and-recovery.md).

A pass at the exact threshold with a lower bound of 0.77 is weak evidence: 40 cases cannot
separate 0.90 from, for example, 0.80. Zero unsafe misses in 40 cases is not zero risk.

## Offline replay (reproducibility check)

`make eval-offline` → `heldout-replay-20261015T022826Z` and `security-replay-…`: strict
replay of the recorded cassettes with network egress limited to loopback. Per-case
verdicts, outcomes and types are identical to the recorded run in both modes;
explanations are labelled `model:replay`, responses `mocked`. A missing or diverged
recording fails the run before anything is sealed
(`tests/integration/test_evaluation.py`).

## Validation and security runs (development, not final evidence)

| Run | Rules only | Mock | Notes |
|---|---|---|---|
| validation-record-…022235Z | 20/20 | 20/20 | 12 of 126 model outputs rejected by validators → 6 template fallbacks |
| validation-record-…022504Z | 20/20 | 20/20 | after fixes: 0 rejected |
| security-record-…022529Z | 7/7 | 7/7 | 6 proposal outputs rejected |
| security-record-…022617Z | 7/7 | 7/7 | after fix: 0 rejected |

Defects these runs found (fixed before the held-out run; earlier runs stay in the ledger):
1. `invoiced_exceeds_ordered` stated a cumulative invoiced quantity that was not in its
   values (and carried another finding's `excess`); the charges note stated an unrecorded
   line count. A faithful restatement of the finding therefore failed numeric validation.
   Rules version → `opsbridge-rules/1.0.1`; invariant test over dev + validation corpora
   (`tests/agents/test_finding_numbers.py`).
2. The numeric-support validator rejected magnitudes of signed differences ("3.00 below")
   and numbers from the rule-selected title template ("1 condition(s)").
3. A harness defect (ledger linking) aborted the first validation attempt before results
   were written; that uncommitted partial output was discarded.

## Receipts

14 sealed records, chain intact, 14 signatures valid, every bound artifact re-hashed `ok`
(`make eval-offline` output). Bound per record: corpus split (dataset), frozen manifest,
acceptance policy, policy files, prompt snapshot, per-trial log, EvalSeal `tool_acl` and
`tool_responses` (agent schemas, validated by EvalSeal), and the model cassette for mock
runs. Tamper detection is tested: editing the trial log reports `changed`; deleting a bound
file reports `missing` and fails verification. A signature by the local key shows the files
are unchanged since sealing; it does not show when runs happened, that inputs were true, or
that no other runs were made.

## Limits

- Corpus: 104 synthetic cases (target ~120 not reached); labels written by the same agent
  that wrote the rules, from the policy text — not an independent business oracle.
- Mock responses only; the agent comparison measures the integration, not a model.
- Latency is local wall time on one machine; no cost was incurred or measured for real models.
- No confidence scores are reported; none are calibrated.
