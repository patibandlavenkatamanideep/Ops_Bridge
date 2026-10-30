"""Model adapters: deterministic mock, Anthropic (official SDK), Ollama (HTTP API).

Verified interfaces (2026-10-14; anthropic 2.1.0 on 2026-10-25):
- anthropic 2.1.0 (API version 2026-10-21): `messages.create(model, max_tokens, system,
  messages, output_config={"format": {"type": "json_schema", "schema": ...}, "effort": ...},
  stop_sequences, timeout)`. The method has no temperature/top_p/seed parameters, so those
  are declared unsupported. Since 2.0 the key is sent as `Authorization: Bearer`.
- Ollama `/api/chat` (docs/api.md): `model`, `messages`, `format` (JSON schema), `options`
  (e.g. `temperature`, `seed`, `num_predict`), `stream: false`; response has `model`,
  `message.content`, `done_reason`, `prompt_eval_count`, `eval_count`. `/api/tags` lists
  local models with digests.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from opsbridge import safejson
from opsbridge.connectors import http_transport

from .contracts import (
    Capabilities,
    ModelRefused,
    ModelRequest,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
    check_params,
)

# ---------------------------------------------------------------------------------------
# Deterministic mock
# ---------------------------------------------------------------------------------------

# Bounded reads from a local model server: its model list, and one non-streamed reply.
OLLAMA_TAGS_MAX_BYTES = 1024 * 1024
OLLAMA_REPLY_MAX_BYTES = 8 * 1024 * 1024
_CONTEXT_RE = re.compile(r"<context_json>\n(.*?)\n</context_json>", re.S)


class MockModelAdapter:
    """Deterministic stand-in. Builds output only from the request's own context JSON.

    It never sees evaluation labels: the context contains deterministic findings and
    evidence, exactly what a real model would receive. Results prove the pipeline and
    interfaces, not language-model quality.
    """

    name = "mock"
    capabilities = Capabilities(
        provider="mock",
        structured_output="native_schema",
        supported_params=frozenset(),
        hosted=False,
        local=True,
        reports_token_usage=False,
        reports_resolved_model=True,
        notes="Deterministic; output derived from the provided context only.",
    )
    model_id = "opsbridge-mock-1"

    def generate(self, request: ModelRequest) -> ModelResponse:
        check_params(request, self.capabilities)
        start = time.monotonic()
        m = _CONTEXT_RE.search(request.user)
        ctx: dict[str, Any] = json.loads(m.group(1)) if m else {}
        if request.template_id.startswith("explain_findings"):
            out = _mock_explain(ctx)
        elif request.template_id.startswith("propose_follow_up"):
            out = _mock_propose(ctx)
        else:
            out = {}
        text = json.dumps(out, sort_keys=True)
        return ModelResponse(
            text=text,
            provider="mock",
            requested_model=self.model_id,
            resolved_model=self.model_id,
            origin="mocked",
            input_tokens=None,
            output_tokens=None,
            latency_ms=int((time.monotonic() - start) * 1000),
            stop_reason="end_turn",
            effective_params={"max_output_tokens": request.max_output_tokens},
            endpoint="local://mock",
            sdk_version=None,
        )


def _mock_explain(ctx: dict[str, Any]) -> dict[str, Any]:
    findings = ctx.get("findings", [])
    statements = []
    for f in findings:
        if f.get("status") == "informational" and f.get("finding_type") == "note":
            continue
        cites = f.get("evidence_labels", [])[:6]
        if cites:
            statements.append({"text": f["summary"], "citations": cites})
    outcome = ctx.get("deterministic_outcome", "manual_review")
    requests = []
    if ctx.get("round", 0) == 0 and any(
        e.get("kind") == "supplier_message" for e in ctx.get("evidence", [])
    ):
        requests.append({"tool": "search_case_evidence", "query": "delivered"})
    return {
        "outcome": {
            "matched": "answer",
            "discrepancies_found": "answer",
            "needs_information": "clarify",
        }.get(outcome, "escalate"),
        "summary": f"{len(statements)} cited statement(s) under {ctx.get('policy_ref', 'the policy')}.",
        "statements": statements,
        "uncertainties": list(ctx.get("uncertainty", []))[:8],
        "evidence_requests": requests,
    }


def _mock_propose(ctx: dict[str, Any]) -> dict[str, Any]:
    rec = ctx.get("rules_recommendation") or {}
    decision = {
        "propose_action": "propose",
        "request_information": "clarify",
        "escalate": "escalate",
        "no_discrepancy": "no_action",
    }.get(rec.get("decision", ""), "escalate")
    body_lines = [s["text"] for s in ctx.get("finding_summaries", [])][:5]
    return {
        "decision": decision,
        "action_type": rec.get("action_type", "assign_case"),
        "assignee_group": rec.get("assignee_group"),
        "title": rec.get("title", "Review case"),
        "body": "\n".join(body_lines) or "See case findings.",
        "citations": ctx.get("all_labels", [])[:6],
        "rationale": "Follows the deterministic route for the reported findings.",
    }


class ScriptedModelAdapter:
    """Test double returning scripted outputs (e.g. a compromised or malformed model)."""

    name = "scripted"
    capabilities = MockModelAdapter.capabilities

    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = list(outputs)
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        out = self.outputs.pop(0) if self.outputs else "{}"
        if isinstance(out, Exception):
            raise out
        return ModelResponse(
            text=out,
            provider="scripted",
            requested_model="scripted",
            resolved_model="scripted",
            origin="mocked",
            input_tokens=None,
            output_tokens=None,
            latency_ms=0,
            stop_reason="end_turn",
            effective_params={},
            endpoint="local://scripted",
            sdk_version=None,
        )


# ---------------------------------------------------------------------------------------
# Anthropic (hosted)
# ---------------------------------------------------------------------------------------


class AnthropicAdapter:
    name = "anthropic"

    def __init__(
        self, model: str, api_key: str, client_factory: Callable[..., Any] | None = None
    ) -> None:
        import anthropic

        if not model:
            raise ModelUnavailable("no Anthropic model id configured (OPSBRIDGE_ANTHROPIC_MODEL)")
        self.model = model
        self._sdk_version = anthropic.__version__
        factory = client_factory or anthropic.Anthropic
        # The SDK traces through the app's global tracer provider. Keep its spans to metadata
        # (an ANTHROPIC_OPEN_TELEMETRY_* variable cannot switch them to prompt and evidence
        # text: an explicit option ignores the environment) and send no trace context to the
        # provider.
        telemetry: anthropic.OpenTelemetryOptions = {
            "propagation": False,
            "traces": {"enabled": True, "content_mode": "metadata_only"},
        }
        self._client: Any = factory(api_key=api_key, max_retries=0, open_telemetry=telemetry)
        self.capabilities = Capabilities(
            provider="anthropic",
            structured_output="native_schema",
            supported_params=frozenset({"effort", "stop_sequences"}),
            hosted=True,
            local=False,
            reports_token_usage=True,
            reports_resolved_model=True,
            notes=f"anthropic SDK {self._sdk_version}; messages.create exposes no temperature/top_p/seed.",
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        import anthropic

        check_params(request, self.capabilities)
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": request.output_schema}
        }
        if "effort" in request.params:
            output_config["effort"] = request.params["effort"]
        kwargs: dict[str, Any] = {}
        if "stop_sequences" in request.params:
            kwargs["stop_sequences"] = request.params["stop_sequences"]
        start = time.monotonic()
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                output_config=output_config,
                timeout=request.timeout_seconds,
                **kwargs,
            )
        except anthropic.APITimeoutError as exc:
            raise ModelTimeout("Anthropic request timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise ModelUnavailable(f"Anthropic connection error: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            raise ModelUnavailable(f"Anthropic returned HTTP {exc.status_code}") from exc
        latency = int((time.monotonic() - start) * 1000)
        text = "".join(
            getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
        )
        if msg.stop_reason in ("refusal",) or not text:
            raise ModelRefused(f"no usable output (stop_reason={msg.stop_reason})")
        return ModelResponse(
            text=text,
            provider="anthropic",
            requested_model=self.model,
            resolved_model=msg.model,
            origin="live",
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            latency_ms=latency,
            stop_reason=msg.stop_reason,
            effective_params={
                "max_tokens": request.max_output_tokens,
                **{k: v for k, v in request.params.items()},
                "sampling": "provider default (not configurable in this SDK method)",
            },
            endpoint="api.anthropic.com/v1/messages",
            sdk_version=self._sdk_version,
        )


# ---------------------------------------------------------------------------------------
# Ollama (local)
# ---------------------------------------------------------------------------------------


class OllamaAdapter:
    name = "ollama"
    capabilities = Capabilities(
        provider="ollama",
        structured_output="native_schema",
        supported_params=frozenset({"temperature", "seed", "num_ctx"}),
        hosted=False,
        local=True,
        reports_token_usage=True,
        reports_resolved_model=True,
        notes="Ollama /api/chat with `format` JSON schema; resolved revision = local model digest.",
    )

    def __init__(
        self, base_url: str, model: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        if not model:
            raise ModelUnavailable("no Ollama model configured (OPSBRIDGE_OLLAMA_MODEL)")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.Client(
            base_url=self.base_url, timeout=60.0, transport=transport, follow_redirects=False
        )

    def available(self) -> tuple[bool, str | None]:
        """Return (available, digest) without generating anything."""
        try:
            r = http_transport.request(
                self._client,
                "GET",
                "/api/tags",
                timeout=2.0,
                max_bytes=OLLAMA_TAGS_MAX_BYTES,
                total_seconds=2.0,
            )
            if r.status_code != 200:
                return False, None
            for m in safejson.response_json(r).get("models", []):
                if m.get("name") == self.model or m.get("model") == self.model:
                    return True, m.get("digest")
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            return False, None
        return False, None

    def generate(self, request: ModelRequest) -> ModelResponse:
        check_params(request, self.capabilities)
        ok, digest = self.available()
        if not ok:
            raise ModelUnavailable(
                f"local model {self.model!r} is not available at {self.base_url}"
            )
        options = {"num_predict": request.max_output_tokens, **request.params}
        body = {
            "model": self.model,
            "stream": False,
            "format": request.output_schema,
            "options": options,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        start = time.monotonic()
        try:
            r = http_transport.request(
                self._client,
                "POST",
                "/api/chat",
                json=body,
                timeout=request.timeout_seconds,
                max_bytes=OLLAMA_REPLY_MAX_BYTES,
                total_seconds=request.timeout_seconds,
            )
        except httpx.TimeoutException as exc:  # the total deadline included
            raise ModelTimeout("Ollama request timed out") from exc
        except httpx.HTTPError as exc:  # an oversized or undecodable reply included
            raise ModelUnavailable(f"Ollama connection error: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ModelUnavailable(f"Ollama returned HTTP {r.status_code}")
        try:
            data = safejson.response_json(r)
            if not isinstance(data, dict):
                raise ValueError("reply is not an object")
        except ValueError as exc:
            raise ModelUnavailable(f"Ollama reply unreadable: {exc}") from exc
        return ModelResponse(
            text=(data.get("message") or {}).get("content", ""),
            provider="ollama",
            requested_model=self.model,
            resolved_model=f"{data.get('model')}@{digest}" if digest else data.get("model"),
            origin="live",
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            latency_ms=int((time.monotonic() - start) * 1000),
            stop_reason=data.get("done_reason"),
            effective_params=options,
            endpoint=f"{self.base_url}/api/chat",
            sdk_version=None,
        )
