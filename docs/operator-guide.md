# Operator guide

For the people who use OpsBridge day to day: investigators, approvers, requesters,
auditors and tenant administrators. Technical runbooks are in docs/operations.md.

## What OpsBridge is for

An invoice is blocked. OpsBridge gathers the purchase order, goods receipts, the invoice and
supplier messages, checks them against your organization's matching policy, and tells you
what does not line up, where it says so, and what the policy says should happen next. It
never pays, posts, changes a purchase order or edits supplier bank details. A follow-up task
is not a resolution: the case stays open until the evidence matches policy.

## Roles

| Role | Can |
|---|---|
| Requester | Open a case and see its outcome (not evidence details) |
| Investigator | Run investigations, read and correct extracted evidence, upload documents, add notes, propose follow-ups |
| Approver | Read evidence, approve or reject proposals within their approval scope, review unknown outcomes |
| Auditor | Read cases, evidence, proposals, operations, evaluation results and settings |
| Tenant administrator | Change settings (default model mode, connector, retention), run retention, delete any note's text |

You only see your own organization. If you belong to two, the selector at the top switches
between them, and your role can differ in each.

## Working a case

1. **Open or find the case.** The queue shows discrepancy type, owner, age, evidence
   readiness and state, and flags cases where a proposal awaits approval or an operation
   needs review; search by case, invoice or PO number, and sort newest or oldest first.
   Views: *Mine* and *Unassigned* (open cases; a case whose owner no longer works here as an
   investigator counts as unassigned), *Awaiting approval*, and *Needs review* (operations
   whose outcome is unknown: the place to start after an outage). Each view, filter and
   order is in the page address, so you can bookmark or share it.
2. **Take the case.** *Take this case* makes you its owner; *Release* gives it back; *Give
   to* hands it to another investigator. If someone else changed the owner since you opened
   the page, your change is refused and the page shows who owns it now. Closed cases keep
   their owner. Every change is in the audit log.
3. **Run the investigation.** *Rules only* uses no model. A model mode (if your organization
   allows it) adds a written explanation and a draft; the decision itself is always made by
   the rules. *Model use* shows which model ran, or why none did (for example, a policy that
   requires local processing).
4. **Read the outcome.**
   - *Matched*: nothing to do beyond a review note.
   - *Discrepancies found*: each finding states the numbers and cites the documents.
   - *Needs information*: something is missing (often a receiving record). Missing does not
     mean "not delivered".
   - *Manual review*: OpsBridge could not evaluate something exactly (unknown unit, other
     currency, similar but different supplier name, unreadable invoice). A person decides.
5. **Check the evidence.** Click a citation to see the exact line, row or field beside the
   value OpsBridge extracted. If an extraction is wrong, use *Correct*: the original stays
   on record, your correction and reason are audited, and you re-run the investigation to
   use it.
6. **Add what is not in a document** in *Conversation* (calls, supplier replies). Avoid bank
   details and personal contact data. You can correct your own notes; deleting removes the
   text but keeps who, when and why.
7. **Upload a document** (PDF, text, CSV, JSON) on the Evidence tab if the supplier sent
   something new. A file with the same name becomes a new version.

Every step can be done with the keyboard alone: Tab moves between controls, the arrow keys
move between the case's tabs, and Enter acts. After an action (running an investigation,
creating, approving or rejecting a proposal, taking or releasing a case) a status line says
what happened and keeps your place there.

## Proposals and approval

An investigator turns the validated draft into a proposal. It shows exactly what will
happen (for example "create a follow-up task for receiving"), what it affects, the evidence
and policy behind it, a fingerprint of the exact content, and when it expires.

An approver approves or rejects **that version**. If anything changes before the task is
sent — the proposal is edited, the evidence or policy changes, your permission is removed,
or the approval expires — it is not sent and a new proposal is needed. Your organization may
require that the approver is not the person who proposed.

## After approval: what the statuses mean

| Status | Meaning | What you do |
|---|---|---|
| Confirmed | The task was created and read back from the task system | Nothing; the case stays open until the evidence resolves |
| Failed | The task system rejected it, or it was verified as never created | Read the category; fix and re-propose if appropriate |
| Outcome unknown — needs review | OpsBridge cannot tell whether the task was created (for example, a timeout and no way to look it up) | Check the task system for the operation marker shown (`opsbridge-op:…`), then record "the task exists" with its reference, or "no task was created". Do not create it again by hand before checking |
| Cancelled before dispatch | Preconditions failed or a cancellation arrived in time | Re-propose if still needed |

Cancelling after a task was sent cannot undo it; record a follow-up instead.

## Settings (tenant administrators)

Settings are split by responsibility; each section has its own address
(`/t/<organization>/settings/<section>`), so you can bookmark or share it.

- **General:** default investigation mode (limited by your service tier) and retention periods
  in days for original documents, notes, model and tool records (blank = keep). The tier
  enables features; it never grants anyone authority or access to other organizations. *Run
  retention sweep* deletes content past its period immediately; deleted originals cannot be
  restored, and the evidence view says so.
- **Task connection:** where follow-up tasks go (connector, repository, credential reference)
  with its version history; change, rotate or revoke it with a reason. *Connector health*
  (administrators and auditors) shows what the connector promises right now (idempotency
  keys, lookup and request deadlines, asked live from the simulator; for GitHub only its
  configuration, because GitHub is not contacted without authorization), what that means
  after an ambiguous create (resend, look up once its deadline has passed, or human review),
  attempts in the last 24 hours by phase and result, and operations waiting for review.
  *Check again* repeats the live check.
- **Policies:** every policy version; *active* is the one in force today, and an
  investigation uses the version in force on the invoice's decision date.
- **People:** members, their roles and approval scopes, and invitations.

## Evaluation (investigators and auditors)

Shows benchmark runs on synthetic data: how often the next action was right, with an
uncertainty range, which cases were missed and whether a person would have caught them, and
whether the results' receipts verify. These are engineering checks with a mock model, not
evidence of how OpsBridge performs on your invoices.

## When something looks wrong

Every error shows a reference (correlation id). Give it to your administrator; it locates
the request in logs and traces without exposing case content.
