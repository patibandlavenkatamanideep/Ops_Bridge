"""Model-assisted explain/propose steps with deterministic validation.

The deterministic findings are authoritative. A model may phrase an explanation and
draft follow-up text; code decides whether that output is acceptable:

- JSON-schema validation of the structured output (bounded sizes, closed enums).
- Every statement cites evidence labels that exist in the context.
- Every number in a statement appears in the cited evidence or the findings.
- No resolution/payment/approval/fraud claims, URLs or email addresses.
- The model outcome must agree with the deterministic outcome class.
- A proposal must keep the rule-selected action type and assignee group and use an
  action type allowed by policy.
- Evidence requests are limited to a read-only, case-scoped search tool with call caps.

Any failure falls back to the template output; the rejected output and reasons are kept
in the model-call record.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import jsonschema

from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.telemetry import metrics, tracing

from .contracts import ModelAdapter, ModelRequest
from .prompts import EXPLAIN_V1, PROPOSE_V1
from .runtime import Recorder, RunBudget, call_model

SEARCH_TOOL = "search_case_evidence"
TOOL_VERSION = "1"
MAX_ROUNDS = 2
_LABEL = re.compile(r"^E\d{1,3}$")
_NUMBER = re.compile(r"(?<![A-Za-z0-9-])\d+(?:[.,]\d+)*(?![A-Za-z0-9])")
_PROHIBITED = re.compile(
    r"(?i)\b(resolved|resolution complete|approved for payment|approve(?:d)? (?:the )?payment|release(?:d)? "
    r"payment|has been paid|is paid|paid in full|fraud\w*|theft|stole|misconduct|kickback)\b"
)
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "summary", "statements", "uncertainties"],
    "properties": {
        "outcome": {"enum": ["answer", "clarify", "escalate"]},
        "summary": {"type": "string", "maxLength": 600},
        "statements": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "citations"],
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 400},
                    "citations": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {"type": "string", "pattern": "^E[0-9]{1,3}$"},
                    },
                },
            },
        },
        "uncertainties": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 300},
        },
        "evidence_requests": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool", "query"],
                "properties": {
                    "tool": {"enum": [SEARCH_TOOL]},
                    "query": {"type": "string", "minLength": 2, "maxLength": 120},
                },
            },
        },
    },
}

PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "action_type", "title", "body", "citations", "rationale"],
    "properties": {
        "decision": {"enum": ["propose", "clarify", "escalate", "no_action"]},
        "action_type": {
            "enum": [
                "create_follow_up_task",
                "assign_case",
                "create_supplier_message_draft",
                "record_review_note",
            ]
        },
        "assignee_group": {"type": ["string", "null"], "maxLength": 64},
        "title": {"type": "string", "minLength": 3, "maxLength": 140},
        "body": {"type": "string", "maxLength": 2000},
        "citations": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "pattern": "^E[0-9]{1,3}$"},
        },
        "rationale": {"type": "string", "maxLength": 500},
    },
}

_OUTCOME_CLASS = {
    "matched": "answer",
    "discrepancies_found": "answer",
    "needs_information": "clarify",
    "manual_review": "escalate",
}
_DECISION_FOR = {
    "propose_action": "propose",
    "request_information": "clarify",
    "escalate": "escalate",
    "no_discrepancy": "no_action",
}


@dataclass
class Evidence:
    label: str
    ref: dict[str, Any]
    kind: str
    title: str | None
    value: str | None
    excerpt: str | None


@dataclass
class AgentContext:
    case: dict[str, Any]
    recon: dict[str, Any]
    policy_out: dict[str, Any]
    policy: ReconciliationPolicy | None
    evidence: list[Evidence] = field(default_factory=list)

    def label_for(self, ref: dict[str, Any]) -> str:
        for e in self.evidence:
            if (e.ref.get("version_id"), e.ref.get("locator"), e.ref.get("field")) == (
                ref.get("version_id"),
                ref.get("locator"),
                ref.get("field"),
            ):
                return e.label
        label = f"E{len(self.evidence) + 1}"
        self.evidence.append(Evidence(label, ref, "field", None, None, None))
        return label

    def add_hit(self, hit: dict[str, Any]) -> str:
        label = f"E{len(self.evidence) + 1}"
        self.evidence.append(
            Evidence(
                label,
                {
                    "document_id": hit["document_id"],
                    "version_id": hit["version_id"],
                    "sha256": hit["sha256"],
                    "locator": hit["locator"],
                    "field": "excerpt",
                    "classification": "internal",
                },
                hit["kind"],
                hit["title"],
                None,
                hit["text"],
            )
        )
        return label

    def payload(self, round_no: int) -> dict[str, Any]:
        findings = []
        for f in self.recon["findings"]:
            labels = [self.label_for(e) for e in f.get("evidence", [])]
            findings.append(
                {
                    "finding_type": f["finding_type"],
                    "status": f["status"],
                    "summary": f["summary"],
                    "values": f.get("values", {}),
                    "evidence_labels": labels,
                }
            )
        return {
            "round": round_no,
            "case_number": self.case.get("case_number"),
            "policy_ref": self.policy_out.get("policy_ref"),
            "deterministic_outcome": self.recon["outcome"],
            "findings": findings,
            "uncertainty": self.recon.get("uncertainty", []),
            "rules_recommendation": self.recon.get("recommendation"),
            "evidence": [
                {
                    "label": e.label,
                    "kind": e.kind,
                    "field": e.ref.get("field"),
                    "locator": e.ref.get("locator"),
                    "title": e.title,
                    "value": e.value,
                }
                for e in self.evidence
            ],
        }


def _render_user(
    template_instructions: str, ctx_payload: dict[str, Any], excerpts: list[Evidence]
) -> str:
    docs = "\n".join(
        f'<untrusted_document id="{e.label}" kind="{e.kind}">\n{(e.excerpt or "")[:600]}\n</untrusted_document>'
        for e in excerpts
        if e.excerpt
    )
    return (
        f"{template_instructions}\n\n<context_json>\n{json.dumps(ctx_payload, sort_keys=True)}\n</context_json>\n"
        + (f"\n{docs}\n" if docs else "")
    )


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUMBER.findall(text):
        try:
            out.add(format(Decimal(m.replace(",", "")).normalize(), "f"))
        except InvalidOperation:
            continue
    return out


def _allowed_numbers(ctx: AgentContext, labels: list[str]) -> set[str]:
    nums: set[str] = set()
    for f in ctx.recon["findings"]:
        for v in (f.get("values") or {}).values():
            # A computed difference such as "-3.0000" may be restated as a magnitude
            # ("3.00 below"); _NUMBER never matches digits after a hyphen, so strip the sign.
            nums |= _numbers(str(v).strip().lstrip("-"))
        nums |= _numbers(f.get("po_line_number") or "")
        for ln in f.get("invoice_line_numbers") or []:
            nums |= _numbers(str(ln))
    for e in ctx.evidence:
        if e.label in labels:
            nums |= (
                _numbers(e.value or "")
                | _numbers(e.excerpt or "")
                | _numbers(e.ref.get("locator", ""))
            )
    nums |= _numbers(ctx.policy_out.get("policy_ref") or "")
    nums |= _numbers(str(ctx.case.get("case_number") or "")) | _numbers(
        str(ctx.case.get("invoice_number") or "")
    )
    return nums


def _common_text_problems(text: str) -> list[str]:
    problems = []
    if _PROHIBITED.search(text):
        problems.append(f"prohibited claim: {_PROHIBITED.search(text).group(0)!r}")  # type: ignore[union-attr]
    if _URL.search(text):
        problems.append("contains a URL")
    if _EMAIL.search(text):
        problems.append("contains an email address")
    return problems


def validate_explanation(ctx: AgentContext, raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        out = json.loads(raw)
        jsonschema.validate(out, EXPLAIN_SCHEMA)
    except (ValueError, jsonschema.ValidationError) as exc:
        return None, [f"schema: {str(exc).splitlines()[0][:200]}"]
    problems: list[str] = []
    known = {e.label for e in ctx.evidence}
    expected = _OUTCOME_CLASS.get(ctx.recon["outcome"], "escalate")
    if out["outcome"] != expected:
        problems.append(
            f"outcome {out['outcome']!r} disagrees with deterministic outcome class {expected!r}"
        )
    for i, st in enumerate(out["statements"]):
        if not st["citations"]:
            problems.append(f"statement {i + 1} has no citation")
            continue
        missing = [c for c in st["citations"] if c not in known]
        if missing:
            problems.append(f"statement {i + 1} cites unknown evidence {missing}")
            continue
        unsupported = _numbers(st["text"]) - _allowed_numbers(ctx, st["citations"])
        if unsupported:
            problems.append(
                f"statement {i + 1} uses numbers not in its evidence: {sorted(unsupported)}"
            )
        problems += [f"statement {i + 1}: {p}" for p in _common_text_problems(st["text"])]
    for text in [out["summary"], *out["uncertainties"]]:
        problems += _common_text_problems(text)
    return (out if not problems else None), problems


def validate_proposal(ctx: AgentContext, raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        out = json.loads(raw)
        jsonschema.validate(out, PROPOSE_SCHEMA)
    except (ValueError, jsonschema.ValidationError) as exc:
        return None, [f"schema: {str(exc).splitlines()[0][:200]}"]
    problems: list[str] = []
    rec = ctx.recon.get("recommendation") or {}
    if ctx.policy is not None and out["action_type"] not in ctx.policy.allowed_actions:
        problems.append(f"action {out['action_type']!r} is not allowed by policy {ctx.policy.ref}")
    if out["action_type"] != rec.get("action_type"):
        problems.append(
            f"action {out['action_type']!r} differs from the rule-selected {rec.get('action_type')!r}"
        )
    if (out.get("assignee_group") or None) != (rec.get("assignee_group") or None):
        problems.append(
            f"assignee group {out.get('assignee_group')!r} differs from the rule-selected "
            f"{rec.get('assignee_group')!r}"
        )
    if out["decision"] != _DECISION_FOR.get(rec.get("decision", ""), "escalate"):
        problems.append(f"decision {out['decision']!r} disagrees with the deterministic decision")
    known = {e.label for e in ctx.evidence}
    if [c for c in out["citations"] if c not in known]:
        problems.append("cites unknown evidence")
    for field_name in ("title", "body", "rationale"):
        problems += [f"{field_name}: {p}" for p in _common_text_problems(out[field_name])]
    allowed = _allowed_numbers(ctx, out["citations"] or [e.label for e in ctx.evidence])
    # The rule-selected title comes from the policy's template (e.g. a condition count).
    allowed |= _numbers(str(rec.get("title") or ""))
    unsupported = _numbers(out["title"] + " " + out["body"]) - allowed
    if unsupported:
        problems.append(f"uses numbers not in the evidence: {sorted(unsupported)}")
    return (out if not problems else None), problems


SearchFn = Callable[[str], list[dict[str, Any]]]


def explain(
    adapter: ModelAdapter,
    ctx: AgentContext,
    *,
    recorder: Recorder,
    budget: RunBudget,
    settings: Any,
    search: SearchFn,
    fallback: Callable[[], dict[str, Any]],
    tool_transport: str = "inprocess",
) -> dict[str, Any]:
    """Model explanation with an optional, capped read-only evidence-search round."""
    problems_all: list[str] = []
    excerpts: list[Evidence] = [e for e in ctx.evidence if e.excerpt]
    for round_no in range(MAX_ROUNDS):
        payload = ctx.payload(round_no)
        request = ModelRequest(
            EXPLAIN_V1.template_id,
            EXPLAIN_V1.digest,
            EXPLAIN_V1.system,
            _render_user(EXPLAIN_V1.instructions, payload, excerpts),
            EXPLAIN_SCHEMA,
            max_output_tokens=1200,
            timeout_seconds=30.0,
        )
        value, problems, call_id = call_model(
            adapter,
            request,
            step="explain_findings",
            recorder=recorder,
            budget=budget,
            settings=settings,
            validate=lambda raw: validate_explanation(ctx, raw),
        )
        if value is None:
            problems_all += problems
            break
        requests = value.get("evidence_requests") or []
        if requests and round_no + 1 < MAX_ROUNDS:
            for req in requests[:1]:
                try:
                    budget.take_tool_call()
                except Exception as exc:  # budget exhausted: answer with what we have
                    problems_all.append(str(exc))
                    break
                started = time.monotonic()
                try:
                    with tracing.span(
                        f"tool {SEARCH_TOOL}",
                        attributes={"opsbridge.tool.transport": tool_transport},
                        step="explain_findings",
                    ):
                        hits = search(req["query"])
                except Exception as exc:  # denied or failed tool calls are data, not crashes
                    denied = type(exc).__name__ == "McpDenied"
                    metrics.TOOL_CALLS.labels(
                        SEARCH_TOOL, "denied" if denied else "error", tool_transport
                    ).inc()
                    metrics.TOOL_DURATION.labels(SEARCH_TOOL).observe(time.monotonic() - started)
                    recorder.tool(
                        step="explain_findings",
                        tool=SEARCH_TOOL,
                        version=TOOL_VERSION,
                        args={"query": req["query"]},
                        response=None,
                        outcome="denied" if denied else "error",
                        transport=tool_transport,
                        error=str(exc)[:300],
                    )
                    problems_all.append(
                        f"evidence search {'denied' if denied else 'failed'}: {exc}"[:200]
                    )
                    break
                metrics.TOOL_CALLS.labels(SEARCH_TOOL, "ok", tool_transport).inc()
                metrics.TOOL_DURATION.labels(SEARCH_TOOL).observe(time.monotonic() - started)
                recorder.tool(
                    step="explain_findings",
                    tool=SEARCH_TOOL,
                    version=TOOL_VERSION,
                    args={"query": req["query"]},
                    response=hits,
                    outcome="ok",
                    transport=tool_transport,
                )
                for hit in hits[:3]:
                    excerpts.append(_excerpt(ctx, hit))
            continue
        text = _render(value, ctx)
        return {
            "text": text,
            "origin": f"model:{adapter.capabilities.provider}",
            "model_call_id": call_id,
            "statements": value["statements"],
            "labels": {e.label: e.ref for e in ctx.evidence},
            "model_uncertainties": value["uncertainties"],
            "rejected": problems_all,
        }
    out = fallback()
    out["rejected"] = problems_all
    out["origin"] = "template_fallback" if problems_all else out["origin"]
    return out


def _excerpt(ctx: AgentContext, hit: dict[str, Any]) -> Evidence:
    ctx.add_hit(hit)
    return ctx.evidence[-1]


def _render(value: dict[str, Any], ctx: AgentContext) -> str:
    lines = [value["summary"]]
    for st in value["statements"]:
        lines.append(f"- {st['text']} [{', '.join(st['citations'])}]")
    if value["uncertainties"]:
        lines.append("Uncertain: " + "; ".join(value["uncertainties"]))
    rec = ctx.recon.get("recommendation")
    if rec:
        lines.append(
            f"Recommended next step (rule-selected): {rec['action_type'].replace('_', ' ')}. "
            "This is follow-up work; it does not resolve the discrepancy."
        )
    return "\n".join(lines)


def propose(
    adapter: ModelAdapter,
    ctx: AgentContext,
    *,
    recorder: Recorder,
    budget: RunBudget,
    settings: Any,
) -> dict[str, Any]:
    """Draft proposal text; validated against the deterministic recommendation."""
    rec = ctx.recon.get("recommendation") or {}
    rules_draft: dict[str, Any] = {
        "action_type": rec.get("action_type"),
        "assignee_group": rec.get("assignee_group"),
        "title": rec.get("title"),
        "body": "\n".join(
            f["summary"] for f in ctx.recon["findings"] if f["status"] != "informational"
        )[:2000],
        "origin": "rules",
        "citations": [],
        "validation": {"accepted": True, "problems": []},
    }
    if not rec:
        return rules_draft
    payload = ctx.payload(0)
    payload["finding_summaries"] = [
        {"text": f["summary"]} for f in ctx.recon["findings"] if f["status"] != "informational"
    ]
    payload["all_labels"] = [e.label for e in ctx.evidence]
    request = ModelRequest(
        PROPOSE_V1.template_id,
        PROPOSE_V1.digest,
        PROPOSE_V1.system,
        _render_user(PROPOSE_V1.instructions, payload, []),
        PROPOSE_SCHEMA,
        max_output_tokens=800,
        timeout_seconds=30.0,
    )
    value, problems, call_id = call_model(
        adapter,
        request,
        step="propose_follow_up",
        recorder=recorder,
        budget=budget,
        settings=settings,
        validate=lambda raw: validate_proposal(ctx, raw),
    )
    if value is None:
        rules_draft["validation"] = {
            "accepted": False,
            "problems": problems,
            "model_call_id": call_id,
            "fallback": "rules",
        }
        return rules_draft
    return {
        "action_type": value["action_type"],
        "assignee_group": value.get("assignee_group"),
        "title": value["title"],
        "body": value["body"],
        "origin": f"model:{adapter.capabilities.provider}",
        "citations": [
            ctx.evidence[int(c[1:]) - 1].ref
            for c in value["citations"]
            if 0 < int(c[1:]) <= len(ctx.evidence)
        ],
        "validation": {"accepted": True, "problems": [], "model_call_id": call_id},
    }
