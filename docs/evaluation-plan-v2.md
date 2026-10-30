# Evaluation plan v2 (prepared, not run)

Preparation for production review findings A09 (does the model help with the work it
actually does) and A10 (independent labels, new data, appropriate statistics). Nothing here has
been run: it needs independent labellers, authorized data, and a model credential with a
budget. It is written down now, before any v2 result exists, so the measures, splits and
sample sizes cannot be chosen after looking at outcomes.

`opsbridge-bench-v1` stays as it is: its results are historical engineering evidence, graded
under scoring 1. Its held-out cases are public and have been replayed repeatedly, so they
cannot support new out-of-sample claims.

## What is compared (A09)

Both arms make the same deterministic decision; by design the model cannot change the next
action, so the next action is not what v2 measures. The arms differ in the explanation and
the draft the operator reads:

| Arm | Explanation and draft |
|---|---|
| Rules + template | the current templated explanation and draft |
| Rules + model | one selected, pinned model's evidence-grounded explanation and draft |

Measured per case, from independent judgements (below):

- **Factual support**: each claim is supported by its cited evidence or finding; claims with
  no support or contradicted by a source are counted, and must not reach an approval screen
  as verified facts.
- **Critical omissions**: a fact the label marks as needed for the decision is missing.
- **Citation entailment**: the cited location says what the claim says, not merely exists.
- **Draft acceptance and edits**: accepted as is, accepted after edits (edit size), rejected.
- **Operator effort**: review time to a correct next action, in operator sessions (A14).
- **Failures**: fallbacks to the template, rejected model outputs, connector and timeout
  errors, every one kept in the denominator.
- **Cost and latency**: per case and per accepted draft, from the provider's usage, under a
  stated price-table version.

Structured claims tied to finding ids and source fields are the primary check; the existing
regular-expression validators stay as additional checks, not as proof of meaning.

## Model identity (A09)

Per call: requested and resolved model, provider revision or fingerprint where the provider
exposes one (recorded as unknown where it does not), request id, effective decoding settings,
stop reason, SDK version, token usage by category, and the price-table version. A mutable tag
looked up separately from the call is not evidence of what generated a response.

## Data and labels (A10)

- **Corpus v2**: new cases, authorized and synthetic or redacted; none of the v1 cases.
- **Labelling**: at least two labellers who did not write the rules or the scenarios, each
  labelling outcome, discrepancy types, next action, the facts an explanation must contain,
  and the facts it must not state. Written instructions are fixed before labelling starts.
- **Disagreement**: recorded, never overwritten. Each disagreement gets an adjudication record
  (case, field, each label, the adjudicator, the decision and its reason). Agreement is
  reported (per field, with a chance-corrected measure).
- **Sequestered test split**: split by supplier and template family, so no family appears in
  both development and test. The test split is not opened until the plan is frozen
  (`opsbridge-bench-v2`, scoring 2) and is run once.

## Statistics (A10)

- The unit is the case; repeats are aggregated per case (scoring 2) and never counted as
  extra cases. Errors stay in denominators.
- Template families are correlated: results are also reported per family, and an interval
  over cases is not a claim of generalization across suppliers.
- **Sample size, chosen in advance**: for a proportion near 0.9, a Wilson 95% interval of
  about ±0.05 needs roughly 140 cases (1.96² × 0.9 × 0.1 / 0.05²). The paired comparison
  needs enough discordant cases for an exact McNemar test to detect the smallest difference
  the owner considers worth paying for; that difference is agreed before the run.
- **Repeats**: a fixed schedule decided before the run (for example 3 per case per arm), or a
  sequential plan with its stopping rule written down in advance.
- A null or inconclusive result is a result and is published as such.

## Blocked on the owner

Independent labellers and their time; authorized case data; a model credential, a budget and
the spend ceiling; operator sessions for the effort measures. Until then v2 stays a plan.
