# ADR 0007: EvalSeal as an evaluation evidence adapter

Status: accepted (2026-10-14). Verified against evalseal 2.2.0 (PyPI, tag v2.2.0, MIT).

## Verified capabilities used
- Python API exported in `evalseal.__all__`: `Dataset`, `Case`, `LocalCallableTarget`,
  `ExactMatchScorer`, `run_eval(..., artifact_paths=...)`, `seal_and_append`, `verify_chain`,
  `generate_keypair`, `sign_head`, `verify_signatures`.
- CLI: `evalseal verify --ledger ... --artifacts` re-hashes bound artifacts and reports
  `ok`/`changed`/`missing`.
- Agent artifact roles `tool_acl` and `tool_responses` with schemas
  `evalseal.agent-tools/1` and `evalseal.agent-tool-responses/1`
  (docs/agent-eval-receipts.md at v2.2.0), validated fail-closed by EvalSeal.

## Decision
- OpsBridge produces the tool manifest and frozen tool-response files itself and binds
  them with `artifact_paths`. Other OpsBridge artifacts (policy, prompt registry, code and
  environment manifest) are bound under opaque roles prefixed `opsbridge_`.
- OpsBridge never edits EvalSeal schemas or invents native receipt fields.
- Signatures use a locally generated Ed25519 key; the receipt documents that this proves
  integrity relative to that key only, not independent time or input truth.
- External timestamp anchoring (`evalseal anchor`) is not used until its trust validation
  is exercised; it is reported as unavailable.

## Implementation notes (Phase 7)
- `evaluation/receipt.py` runs each benchmark mode through `run_eval` with OpsBridge as a
  `LocalCallableTarget` (prompt = case reference, expected = next-action label,
  `ExactMatchScorer`), passes the ledger head as `prev_hash`, seals with `seal_and_append`
  and signs with `sign_head`. The private key lives in `var/evalseal/` (not committed); the
  public key is `evals/keys/opsbridge-eval.pub`.
- `tool_acl` and `tool_responses` are written in EvalSeal's agent schemas and are validated
  by EvalSeal when sealed. Other bound files use `opsbridge_*` roles.
- Artifact verification uses the documented CLI (`evalseal verify --artifacts --index i`).
  The CLI exits non-zero only for `changed`; OpsBridge additionally fails on `missing` and
  `unverifiable`.
- EvalSeal's `flip_rate` (non-majority verdicts / runs) is the instability definition
  reported; the adjacent-transition rate is not used.
