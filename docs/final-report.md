# Final report — OpsBridge (local build)

Date: 2026-10-15 · Branch `feat/opsbridge-foundation` on
github.com/patibandlavenkatamanideep/Ops_Bridge (never merged to `main`) · All data synthetic.

## Where it stands

| Level | Result |
|---|---|
| **Local application complete** | Yes. Phases 0–9 built and verified on one macOS machine (Podman); `make verify` 14/14 gates; clean-clone and clean-container launches reproduced. |
| **Real model validated** | No. No API key, opt-in or local model was available. The agent path is verified with a deterministic mock; real-model quality is *not evaluated*. |
| **External connector validated** | No. The GitHub Issues connector is contract-tested against a fake of the documented API; a live run needs a sandbox repository, a fine-grained token and your authorization. |
| **Staging deployed** | No. An AWS (ECS Fargate, RDS, EFS) plan with sizes, secrets, cost drivers and rollback is in docs/deployment.md; nothing was provisioned. Hosted CI has not run. |
| **Operator pilot measured** | No. A pilot plan exists (docs/pilot-plan.md); no participants, no timings, no savings claims. |

OpsBridge is not production-ready and makes no compliance, fraud-detection or cost-saving
claims.

## What works (locally)

An investigator opens a blocked invoice; OpsBridge fetches the purchase order, receipts,
invoice and supplier messages from a (simulated) purchasing system, matches them with
deterministic Decimal rules under the effective-dated policy, cites every finding to the
exact document line, optionally adds a validated model explanation, and proposes the
policy's next step. A different approver approves that exact version; the worker creates the
follow-up task through an MCP service, reads it back, reconciles ambiguous outcomes or sends
them to human review, and never resolves the case just because a task exists. Two
organizations (furniture and IT hardware) run through the same core with different packs and
policies. Uploads, conversation notes, retention, tenant settings, telemetry, alerts,
backups, containers and an evaluation view are in place. See README.md for screenshots.

## Exact results (2026-10-15)

| Check | Result |
|---|---|
| `make verify` | 14 passed, 0 failed, 0 skipped — backend lint/format/mypy (102 files); unit + contract 243 passed, 2 skipped (live GitHub; one corpus case without a reconcilable invoice); vitest 8; canary 64/64, 0 changes; receipts 18 records ok; migration drift none; promtool rules ok; integration 114 passed (real PostgreSQL roles, Keycloak, simulator HTTP, MCP); browser 19 passed; build ok |
| Clean clone from GitHub | `make doctor`, `setup`, `lint`, `typecheck`, `test` (243 + 8), `eval-canary`, `build` pass; all 18 receipts verify |
| Clean container launch | Empty volumes → all services healthy in 38 s; 12 browser tests passed against the containers |
| Recovery drills | Worker exit → restarted, healthy in 7 s; PostgreSQL restart → API ready in 3 s, worker backed off without restarting; backup → isolated restore verified (counts, RLS, artifact digests, citations; tampering caught) |
| Alert drills | Identity provider down → alert fired after 145 s, resolved 30 s after recovery; worker down → 65 s, resolved 25 s after restart |
| Held-out benchmark (40 cases, mocked responses) | Next action 36/40 = 0.90 (Wilson 95% 0.77–0.96) in rules-only and agent-assisted modes; 4 safe misses (unseen invoice layout), 0 unsafe; gate "pass (interval includes values below the target)"; strict offline replay at the final commit reproduced identical verdicts |
| Demo script | Steps 4 and 6 automated in `frontend/e2e/demo.spec.ts` and passing |

The two skipped unit tests are not failures being hidden: one is the live GitHub test (runs
only with `OPSBRIDGE_RUN_LIVE`), the other a parametrized case with nothing to check.

## Start and demo

```bash
make setup && make infra-up && make migrate && make seed-demo && make dev   # UI http://localhost:5173
# or, everything in containers (stop `make dev` first):
make demo-containers                                                        # UI http://localhost:8000
```

Demo users and password: README.md; the five-minute walkthrough with fault injection:
docs/demo-script.md; day-to-day use: docs/operator-guide.md; runbooks: docs/operations.md.

## Requirement completion

From docs/requirements-traceability.md (28 requirements): **verified_local 24**;
**implemented_unverified 3** — OB-01 discovery assumptions (no interviews), OB-22 GitHub
sandbox connector (no live run), OB-25 handoff outcomes (no pilot); **in_progress 1** — OB-27
bounded compute (per-tenant quotas not implemented). Each row lists its evidence and its
unresolved limits. Nothing is verified_ci, verified_sandbox, deployed_staging or
pilot_validated.

## Defects found and fixed

The most serious: a resend after a timeout created a **duplicate task** when the task tracker
silently stopped honouring idempotency keys while lookup was unavailable — found by the demo
browser test, fixed by discovering connector capabilities (weakest contract when unknown),
covered in-process and through MCP. Others: the worker crashed on a database restart; an
alert rule could never fire; the web framework trusted client trace context; OIDC codes in
access logs; chunked bodies bypassed the size limit; findings stated numbers missing from
their values. Details and reproduction: docs/case-study.md.

## Known limitations

- 104 synthetic benchmark cases, labelled by the same author that wrote the rules; held-out
  pass exactly at the target with a wide interval.
- Supplier invoice layouts outside the pack mappings are escalated, not read; OCR not
  installed; retrieval is lexical; no S3 artifact store; no per-tenant usage quotas.
- Rate limits are per process; no WAF; container root filesystems are writable.
- The development `.env` was uploaded to iCloud before the move out of `~/Desktop`
  (ADR 0009); it holds local-only development secrets — regenerate them if that matters.

## What needs you (external, not done without authorization)

| Action | Unblocks |
|---|---|
| Anthropic API key + explicit live opt-in + spend ceiling, or install Ollama with a pinned model | Real-model evaluation (OB-07 live, real-model quality gate) |
| A GitHub sandbox repository, a fine-grained token (Issues read/write on that repo) and written authorization for writes | Live connector validation (OB-22) |
| Opening a pull request to `main` or dispatching the workflow | Hosted CI evidence (verified_ci) |
| A cloud account and authorization | Staging deployment |
| A pilot owner, participants and approved data | Operator pilot (OB-25 outcomes) |

## Recommended next steps

1. Run hosted CI (open a PR or dispatch) and fix anything environment-specific.
2. With authorization, run the live GitHub sandbox test and a recorded real-model benchmark
   on the frozen held-out split (new benchmark id if settings change).
3. Add the next supplier layout to the furniture pack from real (approved) samples, then
   re-run validation — the held-out misses point there.
4. Implement per-tenant quotas and the staging gaps in docs/deployment.md before any pilot.
