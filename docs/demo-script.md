# Demo script (about five minutes)

Everything shown is synthetic. Before the demo: `make demo` (or `make dev` after
`make setup infra-up migrate seed-demo`), open http://localhost:5173 and keep a terminal at
the repository root. Demo password: `OPSBRIDGE_DEMO_USER_PASSWORD` in `.env`. Use a private
window per user so sessions do not mix.

## 1. The blocked invoice (30 s)

Sign in as **furn-investigator** (Ivy). New case → title "Invoice INV-9001 blocked",
invoice `INV-9001` → Create. Say what the person is trying to do: decide why AP blocked the
invoice and what should happen next, with evidence they can defend.

## 2. Evidence and policy (75 s)

Model: *Rules only* → **Run investigation**. Point out:
- Outcome *discrepancies found*, policy `furniture-ap@v2` and the superseded v1 that was not
  applied.
- Finding: invoice bills 20 chairs, only 18 received — computed by deterministic rules, not
  a model.
- Click the *invoice INV-9001 · pdf:page=1;line=9* evidence chip → Evidence tab with the
  exact PDF line highlighted beside the extracted fields; the CSV receipt row is cited the
  same way. Show *Download original* and the raw digest.
- Re-run with *Deterministic mock model*: the explanation is labelled `model:mock` and every
  statement carries citations; a follow-up task "does not resolve the discrepancy".

## 3. Proposal and separation of duties (60 s)

Proposals & execution → **Create proposal** from the validated draft (version, payload
digest, policy, expiry shown). Ivy has no approve button. Sign in as **furn-approver** (Abe)
in another window → the same proposal shows *Approve / Reject*; approving binds Abe's
decision to that version and digest. The worker creates the task through Casework MCP; the
timeline shows *dispatch → succeeded*, then *readback → succeeded*, status *confirmed*, case
*Follow-up pending* (not *Resolved*).

## 4. Timeout after commit (60 s)

Make the task system commit the next task but drop the response:

```bash
curl -s -X POST http://127.0.0.1:8090/_control/faults -H 'content-type: application/json' \
  -d '{"org":"demo-furniture","route":"tasks.create","mode":"timeout_after_commit"}'
```

Re-run the investigation, create and approve a new proposal (Ivy, then Abe). The timeline
shows the dispatch attempt as *ambiguous failure*, then a reconciliation that finds the task
by its idempotency key → *confirmed*, exactly one task. Then remove reliable lookup:

```bash
curl -s -X POST http://127.0.0.1:8090/_control/capabilities -H 'content-type: application/json' \
  -d '{"org":"demo-furniture","idempotency":false,"search":false}'
curl -s -X POST http://127.0.0.1:8090/_control/faults -H 'content-type: application/json' \
  -d '{"org":"demo-furniture","route":"tasks.create","mode":"timeout_after_commit"}'
```

Approve once more: the operation ends in **outcome unknown — needs review**; nothing is
retried blindly. Abe records what he finds in the task system (exists, with its reference →
confirmed; or absent → failed). Restore: `curl -s -X DELETE http://127.0.0.1:8090/_control/faults`
and the capabilities call with `true, true`.

## 5. Evidence you can verify (45 s)

Evaluation (sidebar) → the held-out *record* run: 36/40 correct next actions with a 95%
interval of 0.77–0.96, four safe misses (an unseen invoice layout), mocked responses only,
real-model quality *not evaluated*. Then show tampering being caught:

```bash
printf ' ' >> evals/runs/opsbridge-bench-v1/heldout-record-20261015T022744Z/trials-mock.jsonl
uv --directory backend run --frozen python -m opsbridge.cli eval-verify   # that record: FAILED (changed)
git checkout -- evals/runs/opsbridge-bench-v1/heldout-record-20261015T022744Z/trials-mock.jsonl
```

Say what a signature does not prove: when the run happened, that inputs were true, or that
no other runs were made.

## 6. Second organization (30 s)

Sign in as **it-investigator** → New case `INV-7002` (Lumen Compute laptops, camelCase JSON
from a different system). *Run investigation* with the organization default: the IT policy
requires local inference and no local model is installed, so *Model use* reads "ollama —
blocked: local inference unavailable … (policy data routing: local_required)" — no hosted
model is used instead — and the deterministic finding (8 of 10 laptops received) still
stands with a template explanation, routed to `it-asset-intake`. Same code; only the pack and policy differ. A furniture user
cannot open this case (404).

## If something goes wrong

- Sign-in loops: check Keycloak at http://localhost:8081 and `make infra-up`.
- Investigation stays queued: the worker is not running (`make dev` output, or
  http://127.0.0.1:9101/readyz).
- Leftover faults: `curl -s -X DELETE http://127.0.0.1:8090/_control/faults`.
