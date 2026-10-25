"""Model adapter contracts: real SDK/HTTP serialization against local fakes (no network)."""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import httpx2
import pytest

from opsbridge.agents.adapters import AnthropicAdapter, MockModelAdapter, OllamaAdapter
from opsbridge.agents.contracts import (
    ModelRefused,
    ModelRequest,
    ModelUnavailable,
    UnsupportedParameter,
)

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def req(
    params: dict[str, Any] | None = None, template: str = "explain_findings/v1", user: str = "hello"
) -> ModelRequest:
    return ModelRequest(
        template, "sha256:" + "0" * 64, "system text", user, SCHEMA, 256, 5.0, params or {}
    )


def anthropic_factory(handler: Any) -> Any:
    """Real SDK client whose transport is a local handler (the anthropic SDK uses httpx2)."""

    def factory(**kwargs: Any) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            base_url="http://anthropic.test",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            **kwargs,
        )

    return factory


def test_anthropic_request_uses_structured_output_and_maps_usage() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")  # SDK 2.x: the key as a bearer token
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-resolved-test",
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 7},
            },
        )

    adapter = AnthropicAdapter(
        "claude-configured-test", "test-key", client_factory=anthropic_factory(handler)
    )
    resp = adapter.generate(req({"effort": "low"}))
    body = seen["body"]
    assert seen["url"].endswith("/v1/messages") and seen["auth"] == "Bearer test-key"
    assert adapter._client.open_telemetry["propagation"] is False
    assert adapter._client.open_telemetry["traces"]["content_mode"] == "metadata_only"
    assert body["model"] == "claude-configured-test" and body["max_tokens"] == 256
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA},
        "effort": "low",
    }
    assert "temperature" not in body
    assert (resp.text, resp.resolved_model, resp.input_tokens, resp.output_tokens, resp.origin) == (
        '{"ok": true}',
        "claude-resolved-test",
        120,
        7,
        "live",
    )


def test_anthropic_rejects_unsupported_sampling_parameters_without_calling() -> None:
    calls: list[httpx2.Request] = []
    adapter = AnthropicAdapter(
        "m",
        "k",
        client_factory=anthropic_factory(lambda r: calls.append(r) or httpx2.Response(500)),
    )
    with pytest.raises(UnsupportedParameter, match="temperature"):
        adapter.generate(req({"temperature": 0}))
    assert calls == []


def test_anthropic_errors_map_to_contract_categories() -> None:
    a = AnthropicAdapter(
        "m",
        "k",
        client_factory=anthropic_factory(
            lambda r: httpx2.Response(
                529,
                json={
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "overloaded"},
                },
            )
        ),
    )
    with pytest.raises(ModelUnavailable):
        a.generate(req())
    refusal = AnthropicAdapter(
        "m",
        "k",
        client_factory=anthropic_factory(
            lambda r: httpx2.Response(
                200,
                json={
                    "id": "x",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "content": [],
                    "stop_reason": "refusal",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            )
        ),
    )
    with pytest.raises(ModelRefused):
        refusal.generate(req())


def test_anthropic_requires_configured_model_id() -> None:
    with pytest.raises(ModelUnavailable, match="no Anthropic model id"):
        AnthropicAdapter("", "k")


def test_ollama_chat_contract_and_digest_as_resolved_revision() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "local-test:1", "digest": "abc123"}]}
            )
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "local-test:1",
                "message": {"role": "assistant", "content": '{"ok": true}'},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 30,
                "eval_count": 5,
            },
        )

    adapter = OllamaAdapter(
        "http://ollama.test", "local-test:1", transport=httpx.MockTransport(handler)
    )
    resp = adapter.generate(req({"temperature": 0, "seed": 7}))
    body = seen["body"]
    assert body["stream"] is False and body["format"] == SCHEMA
    assert body["options"] == {"num_predict": 256, "temperature": 0, "seed": 7}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert resp.resolved_model == "local-test:1@abc123" and resp.output_tokens == 5
    with pytest.raises(UnsupportedParameter):
        adapter.generate(req({"effort": "high"}))


def test_ollama_unavailable_model_is_reported_not_substituted() -> None:
    adapter = OllamaAdapter(
        "http://ollama.test",
        "missing:1",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"models": [{"name": "other:1", "digest": "d"}]})
        ),
    )
    assert adapter.available() == (False, None)
    with pytest.raises(ModelUnavailable):
        adapter.generate(req())


def test_mock_is_deterministic_and_derived_only_from_context() -> None:
    ctx = {
        "deterministic_outcome": "discrepancies_found",
        "policy_ref": "p@v1",
        "uncertainty": [],
        "findings": [
            {
                "finding_type": "quantity_mismatch",
                "status": "discrepancy",
                "summary": "S1",
                "evidence_labels": ["E1"],
            }
        ],
        "evidence": [{"label": "E1", "kind": "goods_receipt"}],
    }
    user = f"instructions\n\n<context_json>\n{json.dumps(ctx)}\n</context_json>\n"
    a, b = MockModelAdapter().generate(req(user=user)), MockModelAdapter().generate(req(user=user))
    assert a.text == b.text and a.origin == "mocked"
    out = json.loads(a.text)
    assert out["statements"] == [{"text": "S1", "citations": ["E1"]}] and out["outcome"] == "answer"
