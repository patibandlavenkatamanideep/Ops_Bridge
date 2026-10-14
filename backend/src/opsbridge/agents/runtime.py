"""Routing, budgets and recording for model and tool calls.

Routing rules (checked before any data leaves the process):
- `rules_only` → no model.
- policy `human_only` → no model.
- policy `local_required` → only a local adapter; if it is unavailable the model path is
  blocked (no hosted fallback) and the investigation continues with deterministic output.
- hosted adapters additionally require: tier entitlement, explicit live opt-in, a positive
  spend ceiling, configured prices (so the ceiling can be enforced), a model id and a key.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from opsbridge.auth.permissions import TierEntitlements
from opsbridge.canonical import digest
from opsbridge.config import Settings
from opsbridge.evidence.storage import ArtifactStore
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Artifact, ModelCall, ToolInvocation
from opsbridge.privacy.redaction import redact

from .adapters import AnthropicAdapter, MockModelAdapter, OllamaAdapter
from .contracts import (
    BudgetExceeded,
    ModelAdapter,
    ModelError,
    ModelRequest,
    ModelResponse,
    RoutingBlocked,
)


@dataclass
class RoutingDecision:
    mode: str
    adapter: ModelAdapter | None
    reason: str
    blocked: bool = False


def route(
    mode: str,
    tier: TierEntitlements,
    data_routing: str,
    settings: Settings,
    adapter_overrides: dict[str, ModelAdapter] | None = None,
) -> RoutingDecision:
    overrides = adapter_overrides or {}
    if mode == "rules_only":
        return RoutingDecision(mode, None, "rules-only run requested")
    if data_routing == "human_only":
        return RoutingDecision(
            mode, None, "policy routes this data to humans only; no model used", blocked=True
        )
    if mode not in tier.model_modes:
        return RoutingDecision(
            mode, None, f"model mode {mode!r} is not enabled for the {tier.tier} tier", True
        )
    if mode == "mock":
        return RoutingDecision(
            mode, overrides.get("mock") or MockModelAdapter(), "deterministic mock (mocked)"
        )
    if mode == "ollama":
        if not tier.local_inference:
            return RoutingDecision(mode, None, "local inference is not enabled for this tier", True)
        try:
            adapter = overrides.get("ollama") or OllamaAdapter(
                settings.ollama_base_url, settings.ollama_model or ""
            )
        except ModelError as exc:
            return RoutingDecision(mode, None, f"local inference unavailable: {exc}", True)
        if isinstance(adapter, OllamaAdapter) and not adapter.available()[0]:
            return RoutingDecision(
                mode,
                None,
                "local inference unavailable: model not reachable; no hosted fallback",
                True,
            )
        return RoutingDecision(mode, adapter, "local model")
    if mode == "anthropic":
        if data_routing == "local_required":
            return RoutingDecision(
                mode, None, "policy requires local inference; hosted model refused", True
            )
        problems = []
        if not settings.live_model_opt_in:
            problems.append("live model opt-in is off")
        if Decimal(settings.live_spend_ceiling_usd or "0") <= 0:
            problems.append("no spend ceiling configured")
        if not (
            settings.anthropic_price_input_per_mtok_usd
            and settings.anthropic_price_output_per_mtok_usd
        ):
            problems.append("prices not configured, so the ceiling cannot be enforced")
        if not settings.anthropic_model:
            problems.append("no model id configured")
        if "anthropic" not in overrides and not _anthropic_key():
            problems.append("no API key")
        if problems:
            return RoutingDecision(mode, None, "hosted model blocked: " + "; ".join(problems), True)
        adapter = overrides.get("anthropic") or AnthropicAdapter(
            settings.anthropic_model or "", _anthropic_key()
        )
        return RoutingDecision(mode, adapter, "hosted model (live, opted in)")
    return RoutingDecision(mode, None, f"unknown model mode {mode!r}", True)


def _anthropic_key() -> str:
    import os

    return os.environ.get("ANTHROPIC_API_KEY", "")


@dataclass
class RunBudget:
    max_model_calls: int
    max_tool_calls: int
    max_output_tokens: int
    timeout_seconds: float
    model_calls: int = 0
    tool_calls: int = 0

    def take_model_call(self) -> None:
        if self.model_calls >= self.max_model_calls:
            raise BudgetExceeded(f"model call cap {self.max_model_calls} reached for this run")
        self.model_calls += 1

    def take_tool_call(self) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(f"tool call cap {self.max_tool_calls} reached for this run")
        self.tool_calls += 1


@dataclass
class Recorder:
    """Persists model calls and tool invocations, with request/response artifacts."""

    db: Database
    store: ArtifactStore
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    tool_seq: int = 0
    model_call_ids: list[str] = field(default_factory=list)

    def _artifact(self, s: Any, kind: str, payload: Any) -> uuid.UUID:
        data = (
            payload
            if isinstance(payload, str)
            else __import__("json").dumps(payload, sort_keys=True)
        ).encode()
        key, sha = self.store.put(self.tenant_id, data)
        art = Artifact(
            tenant_id=self.tenant_id,
            kind=kind,
            sha256=sha,
            size_bytes=len(data),
            content_type="application/json",
            storage_key=key,
            classification="internal",
        )
        s.add(art)
        s.flush()
        return art.id

    def model_call(
        self,
        *,
        step: str,
        request: ModelRequest,
        sent_user: str,
        response: ModelResponse | None,
        outcome: str,
        error: str | None,
        redaction: dict[str, Any],
        provider: str,
        requested_model: str | None,
        cost_estimate: Decimal | None,
        latency_ms: int,
        validation: dict[str, Any] | None = None,
    ) -> str:
        with self.db.session(DbContext(tenant_id=self.tenant_id)) as s:
            req_payload = {
                "template_id": request.template_id,
                "system": request.system,
                "user": sent_user,
                "output_schema": request.output_schema,
                "max_output_tokens": request.max_output_tokens,
                "params": request.params,
            }
            self._artifact(s, "model_request", req_payload)
            if response is not None:
                self._artifact(s, "model_response", {"text": response.text})
            row = ModelCall(
                tenant_id=self.tenant_id,
                run_id=self.run_id,
                step_name=step,
                provider=provider,
                requested_model=requested_model,
                resolved_model=response.resolved_model if response else None,
                endpoint=response.endpoint if response else None,
                sdk_version=response.sdk_version if response else None,
                prompt_template_id=request.template_id,
                prompt_template_digest=request.template_digest,
                input_digest=digest(req_payload),
                output_digest=digest({"text": response.text}) if response else None,
                params={
                    "requested": {"max_output_tokens": request.max_output_tokens, **request.params},
                    "effective": response.effective_params if response else None,
                    "provider_defaults": "unknown unless reported by the provider",
                    "validation": validation or {},
                },
                input_tokens=response.input_tokens if response else None,
                output_tokens=response.output_tokens if response else None,
                cost_estimate_usd=cost_estimate,
                latency_ms=latency_ms,
                outcome=outcome,
                error=error,
                origin=response.origin if response else "live",
                redaction=redaction,
            )
            s.add(row)
            s.flush()
            self.model_call_ids.append(str(row.id))
            return str(row.id)

    def tool(
        self,
        *,
        step: str,
        tool: str,
        version: str,
        args: dict[str, Any],
        response: Any,
        outcome: str,
        origin: str = "live",
        transport: str = "inprocess",
        error: str | None = None,
    ) -> None:
        with self.db.session(DbContext(tenant_id=self.tenant_id)) as s:
            self.tool_seq += 1
            req_id = self._artifact(s, "tool_request", {"tool": tool, "args": args})
            resp_id = self._artifact(s, "tool_response", response) if response is not None else None
            s.add(
                ToolInvocation(
                    tenant_id=self.tenant_id,
                    run_id=self.run_id,
                    step_name=step,
                    seq=self.tool_seq,
                    tool_name=tool,
                    tool_version=version,
                    transport=transport,
                    input_digest=digest(args),
                    response_digest=digest(response) if response is not None else None,
                    outcome=outcome,
                    origin=origin,
                    request_artifact_id=req_id,
                    response_artifact_id=resp_id,
                    error=error,
                )
            )


def spent_live_usd(db: Database, tenant_id: uuid.UUID) -> Decimal:
    with db.session(DbContext(tenant_id=tenant_id)) as s:
        total = s.execute(
            select(func.coalesce(func.sum(ModelCall.cost_estimate_usd), 0)).where(
                ModelCall.origin == "live"
            )
        ).scalar_one()
        return Decimal(str(total or 0))


def call_model(
    adapter: ModelAdapter,
    request: ModelRequest,
    *,
    step: str,
    recorder: Recorder,
    budget: RunBudget,
    settings: Settings,
    validate: Callable[[str], tuple[Any, list[str]]],
) -> tuple[Any | None, list[str], str]:
    """Run one bounded model call with redaction, budget, cost ceiling, recording and validation.

    Returns (validated_output or None, problems, model_call_id). Never raises ModelError:
    failures are recorded and returned as problems so the caller can fall back.
    """
    caps = adapter.capabilities
    sent_user = request.user
    redaction: dict[str, Any] = {"applied": False}
    if caps.hosted:
        rr = redact(request.user)
        sent_user = rr.text
        redaction = {"applied": True, **rr.metadata}
    req = ModelRequest(
        request.template_id,
        request.template_digest,
        request.system,
        sent_user,
        request.output_schema,
        min(request.max_output_tokens, budget.max_output_tokens),
        request.timeout_seconds,
        request.params,
    )
    cost: Decimal | None = None
    start = time.monotonic()
    try:
        budget.take_model_call()
        if caps.hosted:
            cost = _estimate_cost(req, settings)
            ceiling = Decimal(settings.live_spend_ceiling_usd or "0")
            if spent_live_usd(recorder.db, recorder.tenant_id) + cost > ceiling:
                raise BudgetExceeded(
                    f"estimated cost {cost} USD would exceed the live spend ceiling {ceiling} USD"
                )
        response = adapter.generate(req)
    except ModelError as exc:
        cid = recorder.model_call(
            step=step,
            request=req,
            sent_user=sent_user,
            response=None,
            outcome=exc.category,
            error=str(exc),
            redaction=redaction,
            provider=caps.provider,
            requested_model=getattr(adapter, "model", None),
            cost_estimate=None,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        return None, [f"{exc.category}: {exc}"], cid
    if len(response.text) > req.max_output_tokens * 8:
        problems = ["output exceeds the size bound"]
        value = None
    else:
        value, problems = validate(response.text)
    cid = recorder.model_call(
        step=step,
        request=req,
        sent_user=sent_user,
        response=response,
        outcome="ok" if not problems else "rejected_output",
        error="; ".join(problems) or None,
        redaction=redaction,
        provider=caps.provider,
        requested_model=response.requested_model,
        cost_estimate=cost if caps.hosted else None,
        latency_ms=response.latency_ms,
        validation={"accepted": not problems, "problems": problems},
    )
    return (value if not problems else None), problems, cid


def _estimate_cost(req: ModelRequest, settings: Settings) -> Decimal:
    if not (
        settings.anthropic_price_input_per_mtok_usd and settings.anthropic_price_output_per_mtok_usd
    ):
        raise RoutingBlocked("prices not configured")
    # Upper-bound estimate: ~4 characters per token for input, full output allowance.
    in_tokens = Decimal(len(req.system) + len(req.user)) / 4
    out_tokens = Decimal(req.max_output_tokens)
    return (
        in_tokens * Decimal(settings.anthropic_price_input_per_mtok_usd)
        + out_tokens * Decimal(settings.anthropic_price_output_per_mtok_usd)
    ) / Decimal(1_000_000)
