# Evaluation plan — benchmark `opsbridge-bench-v1`

Written and frozen on 2026-10-14 **before any OpsBridge run on the held-out split**. The
frozen inputs are listed by digest in `evals/manifests/opsbridge-bench-v1.json`; the
commit that adds this file, the corpus and the manifest precedes the first held-out run in
git history. Results go to docs/evaluation-results.md and never edit this plan.

## Question

On cases it has not been tuned on, does OpsBridge reach the correct next action for a
blocked invoice — and does adding the bounded agent steps change that, or the evidence it
shows? Secondary: where it is wrong, does it fail safe (escalate to a person)?

## Corpus and splits

| Split | Cases | Use | Families / templates |
|---|---|---|---|
| dev | 37 | rule and mapping development (labels and rules co-written; not evidence of quality) | 17 / 5 |
| validation | 20 | tuning and defect discovery; code may change after it is run | 14 / 3 |
| heldout | 40 | final comparison, run once per mode after freezing | 26 / 5 |
| security | 7 | injection, unreadable and defective documents, double counting; reported separately | 4 / 2 |

- Total 104 synthetic cases, short of the brief's design target of about 120; the counts
  are design choices, not a statement of statistical power.
- Splits are by scenario family and supplier/document template: no family or template
  appears in two splits (checked by `opsbridge.evaluation.manifest.inputs`).
- Held-out deliberately includes variations no rule or mapping was written for: an unseen
  supplier invoice layout (`har-pdf-v2`), effective-dated policy (invoices dated under the
  superseded furniture policy v1), invoices against cancelled POs, rejected receipt lines,
  billing beyond the ordered quantity, unit aliases, pack-unit price normalization, PO line
  discounts, multi-receipt sequences with returns, and an unsupported currency.
- Labels are typed by hand from the policy files and the documented decision rules
  (`fixtures/scenarios/author_eval.py`). They were written by the same author (an AI coding
  agent) who wrote the rules, which limits independence: they are an oracle derived from
  the written policy, not from a separate business owner. No labels came from running
  OpsBridge.

## Systems compared (same cases, same inputs, same code path)

1. `rules_only` — deterministic matching plus templated guidance.
2. `mock` — agent-assisted: the same run plus explanation and proposal steps with the
   deterministic mock model, validated by the same validators.

Both run through the product path: simulator HTTP (loopback) → ingestion into PostgreSQL
(`opsbridge_eval`) → worker workflow → investigation record. By design the next action is
rule-selected and model proposals must agree with it, so next-action accuracy is expected
to be identical; the comparison measures whether agent assistance introduces errors,
rejected outputs, invalid citations, or fallbacks. Real models (`anthropic`, `ollama`) are
**not evaluated** in this benchmark: no credentials, opt-in or local model are available.

## Repeats, replay and settings

- Fixed schedule: 3 repeats per case per mode. No sequential stopping.
- Agent mode records model responses into a cassette (`evals/cassettes/…`, origin
  `mocked`). The offline benchmark (`make eval-offline`) replays it strictly: requests are
  matched by case, repeat, step, occurrence and request digest; an unmatched call fails the
  run (it never falls back to a live call); Python-level network egress to anything other
  than loopback is blocked during the run.
- Agent tool calls (the read-only evidence search) are recorded in a tool cassette beside
  the model cassette (`tool-cassette.json`, `opsbridge-tool-cassette/1`): keyed by case,
  mode, repeat and call occurrence, checked against tool name, version and argument digest.
  Replay serves the recorded response and does not run the tool; a recorded denial or error
  replays as that outcome. Recorded hits name database rows of the recording run, so replay
  re-binds each hit to this run's document version with the same content digest; a hit
  with no single match fails the run. Replayed tool calls are labelled `recorded` in the
  trial log and in EvalSeal's `tool_acl`/`tool_responses`.
- Frozen external state: the simulator is seeded from the split's corpus file only.

## Metrics

Primary: case-level correct next action (`decision:action_type`), majority over repeats,
denominator = all cases in the split (errors count as incorrect), with a Wilson 95%
interval over cases. Secondary: outcome and discrepancy-type correctness, case
correctness, per-type precision/recall with counts, safe misses, instability (EvalSeal
`flip_rate` = non-majority verdicts / repeats), and for agent mode: explanation origin,
rejected model outputs, citation validity. Rules-vs-agent comparison is paired by case
(discordant counts, exact McNemar). Latency is wall time per run on the local machine;
cost is zero for mock and not measured otherwise.

Repeated trials of one case are not treated as independent cases. With 40 held-out cases a
Wilson interval is wide; an inconclusive result will be reported as such. Instability is
reported with its sampling limit: with *r* repeats per case, a per-case flip rate below 1/*r*
cannot be observed, and no flips do not show a case is stable (added 2026-10-19; it does not
change the frozen scoring definitions).

Comparability: the frozen plan includes the scoring definitions. If the code's scoring
differs from the frozen plan, a benchmark run refuses to start, and the evaluation view marks
runs graded under another plan or scoring as *not comparable*: a change of grading, not
evidence of a model or rules regression (`tests/evaluation/test_comparability.py`).

## Acceptance

`evals/acceptance-policy.json` (frozen with this plan): security/recovery invariants pass;
no unauthorized writes and no duplicate external effects in the declared trial set;
held-out next-action accuracy ≥ 0.90 on the point estimate, reported with its interval;
real-model quality gate `not_evaluated` without real-model data. The threshold is not
lowered after results are seen; a failed or inconclusive gate is reported with its
findings.

## Receipt

Each benchmark run is executed through EvalSeal 2.2.0 (`run_eval` with OpsBridge as a
`LocalCallableTarget`, `ExactMatchScorer` on the next-action label), sealed into
`evals/ledger/opsbridge-bench.jsonl`, and signed with a locally generated Ed25519 key whose
public half is committed. Bound artifacts: the corpus split, this manifest, the acceptance
policy, policy and pack files, the prompt registry, the per-trial log, the model and tool
cassettes, an EvalSeal tool manifest (`tool_acl`) and frozen tool responses (`tool_responses`). A
signature proves integrity relative to that key only — not when the run happened, that
inputs were true, or that no other runs occurred. External timestamping is not used.
