# Agent steps and model adapters

Status: implemented and **verified locally with the deterministic mock and contract
fakes only**. No live hosted or local model has been called (no credentials, no opt-in,
Ollama not installed). Mock results prove execution paths and interfaces; they say
nothing about language-model quality.

## Workflow steps (durable, resumable)

| Step | Model? | What code guarantees |
|---|---|---|
| collect_context | no | Authorized source fetch → versioned evidence |
| retrieve_policy | no | Policy version effective on the invoice date |
| reconcile_records | **never** | Deterministic findings and rule-selected next action |
| explain_findings | optional | Validated statements with citations, or template fallback |
| propose_follow_up | optional | Draft text only; action type and assignee fixed by rules |
| validate_proposal | no | Policy gate on the draft (allowed action, defined group, bounds) |

Completed steps are not re-run after a worker restart, so model calls are not repeated
on resume. A run records routing, budget and usage in `workflow_runs`.

## Adapter contract (`backend/src/opsbridge/agents/`)

| Adapter | Origin label | Structured output | Supported params | Verified how |
|---|---|---|---|---|
| `MockModelAdapter` | mocked | n/a (builds JSON from its own context) | none | unit + integration + browser |
| `AnthropicAdapter` (anthropic 2.1.0) | live | `output_config.format` JSON schema | `effort`, `stop_sequences` | real SDK serialization against a local fake transport; **no live call** |
| `OllamaAdapter` (`/api/chat`) | live | `format` JSON schema | `temperature`, `seed`, `num_ctx` | HTTP contract against a local fake; **Ollama not installed** |

Unsupported parameters raise `UnsupportedParameter` before any request (e.g.
`temperature` for the Anthropic SDK method, whose signature has no sampling
parameters). Recorded per call: provider, requested model, resolved model (provider
`model` field; Ollama model digest), endpoint without secrets, SDK version, prompt
template id + digest, input/output digests, requested and effective parameters,
"provider defaults: unknown unless reported", token counts when reported, latency,
outcome, origin, redaction metadata. Request/response bodies are stored as protected
artifacts.

## Routing (checked before any data leaves the process)

- `rules_only` or policy `human_only` → no model.
- Policy `local_required` (IT pack) → only a local adapter; if unavailable the model path
  is **blocked** and the run continues with deterministic output and an uncertainty note.
  A hosted request is refused, never used as a fallback.
- Hosted model → requires tier entitlement, `OPSBRIDGE_LIVE_MODEL_OPT_IN=true`, a positive
  `OPSBRIDGE_LIVE_SPEND_CEILING_USD`, configured per-token prices, a model id and a key.
  The estimated cost (upper bound) is checked against the ceiling before each call.
- Tier entitlements enable modes; they never grant permissions (`not_entitled` → 409).

## Validation of model output (deterministic, fail closed)

Explanations: JSON schema (closed enums, bounded sizes, no extra fields); every statement
cites evidence labels that exist; every number in a statement appears in the cited
evidence or findings; no resolution/payment/approval/fraud claims, URLs or email
addresses; model outcome class must match the deterministic outcome. Proposals:
schema-closed action enum; action type, assignee group and decision must equal the
rule-selected route; policy allow-list; same text checks. Rejected output is recorded
with reasons and replaced by template text.

Evidence requests: one read-only, case-scoped search tool (`search_case_evidence`),
at most one request per round, two rounds, tool-call cap per run; recorded as
`tool_invocations` with request/response digests. Write tools are never offered to
the model.

## Untrusted content

Document excerpts reach the model only inside `<untrusted_document>` tags, with a system
rule that instructions in data are ignored. This is a mitigation, not a guarantee. What
limits the damage is that the model has no authority (it cannot choose the route, act,
approve or change permissions) and that the validators above reject outputs that cite
unknown evidence, use unsupported numbers, make prohibited claims or disagree with the
rules (see `tests/integration/test_agent_flow.py::test_injected_document_instructions_cannot_change_authority`,
which uses a deliberately compromised scripted model). A manipulated explanation that passes
validation can still mislead a reader; the human approval step is the remaining control.

## Redaction

`privacy/redaction.py` masks emails, IBANs, Luhn-valid card numbers, US SSNs, phone
numbers and labelled account numbers before hosted calls. It is pattern-based and is not
anonymization; names and free-text addresses can pass through.

## How to run a live check (not done; requires owner authorization)

```bash
export ANTHROPIC_API_KEY=...                       # never committed
OPSBRIDGE_LIVE_MODEL_OPT_IN=true OPSBRIDGE_LIVE_SPEND_CEILING_USD=1.00 \
OPSBRIDGE_ANTHROPIC_MODEL=<model id> \
OPSBRIDGE_ANTHROPIC_PRICE_INPUT_PER_MTOK_USD=<price> OPSBRIDGE_ANTHROPIC_PRICE_OUTPUT_PER_MTOK_USD=<price> \
make dev   # then run an investigation with model mode "anthropic" on an enhanced-tier tenant
```
