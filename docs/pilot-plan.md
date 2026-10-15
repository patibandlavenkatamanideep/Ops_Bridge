# Pilot plan (proposed; not started)

No pilot has been run, no participants recruited and no operator measurements exist. This
plan is what a pilot owner would agree to before one starts; thresholds below are proposals
for the pilot owner to change and sign off **before** any session.

## Goal and question

Does OpsBridge help AP or procurement investigators reach a correct, evidenced next action
for blocked invoices, compared with how they work today, without unsafe actions? The pilot
measures people doing their real task; it does not re-run the synthetic benchmark.

## Scope

- One organization, one purchasing context (pick the one with the most blocked invoices of
  the three types: missing receipt, quantity mismatch, price mismatch).
- 4–6 investigators and 2–3 approvers who already handle these exceptions.
- 2–4 weeks; follow-up tasks go to a sandbox or a dedicated project in the task system, not
  to production queues, unless the pilot owner authorizes otherwise in writing.

## Prerequisites (each must be done before the first session)

1. Written authorization from the pilot owner, including what data may be used.
2. Read-only access to the purchasing/ERP exports or API for the pilot scope, through a
   service account with least privilege; no write access to purchasing or payments.
3. Pack mapping and policy for the organization's formats and tolerances, reviewed by an
   AP lead (docs/integration-onboarding.md); validation cases from their own past exceptions,
   labelled by them, run before go-live.
4. Identity: the organization's OIDC provider, with pilot users and roles.
5. Model use decided: rules only, a local model, or a hosted model with opt-in, a spend
   ceiling and a data-processing agreement; redaction is pattern-based, not anonymization.
6. Hosting agreed (docs/deployment.md), encrypted storage, backups, retention periods,
   restore drill done on synthetic data.

## Consent and data boundaries

Participants consent to session observation and timing; they can stop at any time. Only
cases within the authorized scope are used. No raw documents leave the agreed environment;
measurement notes record case ids, times and outcomes, not document content. Results are
reported in aggregate.

## Tasks

Each participant works a mix of real blocked invoices (target 20 per participant across the
three types), half with their current process and half with OpsBridge, order randomized per
participant, comparable cases paired by type where possible. Approvers review proposals as
they normally would.

## Measures (defined before the pilot)

| Measure | Definition |
|---|---|
| Correct next action | Matches an AP lead's independent decision, recorded before seeing OpsBridge output |
| Time to a correct investigation | Start of work to a decision the lead agrees with, per case |
| Time to approved follow-up | Decision to approved follow-up (OpsBridge arm) |
| Escalations | Manual-review / needs-information outcomes and whether the lead agreed |
| Unsafe outcomes | Wrong action approved; duplicate or unintended external effect; access outside scope — target zero |
| Extraction corrections | Fields corrected by investigators, by document type |
| Usability | Short survey after each session; open feedback |

Report per measure with counts, denominators and ranges; paired by participant. With this
size, differences are indicative, not statistically conclusive.

## Launch criteria for wider use (proposal, to be agreed)

- Zero unsafe outcomes in the pilot.
- Correct next action at least as often as the current process, by the AP lead's judgement.
- Median time to a correct investigation not worse than the current process.
- Every escalation explainable from the case evidence.
- Operators can complete the operator guide tasks without help after one session.

## Feedback and stopping

Weekly review with the pilot owner: defects, mapping gaps, policy questions. Stop or pause
on any unsafe outcome, data handling concern, or participant request.
