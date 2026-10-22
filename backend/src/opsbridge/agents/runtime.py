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

import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text

from opsbridge.auth.permissions import TierEntitlements
from opsbridge.canonical import digest
from opsbridge.config import Settings
from opsbridge.evidence.blobs import store_bytes
from opsbridge.evidence.storage import ArtifactStore
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Artifact, ModelCall, ToolInvocation
from opsbridge.privacy.redaction import redact
from opsbridge.telemetry import metrics, tracing

from .adapters import AnthropicAdapter, MockModelAdapter, OllamaAdapter
from .contracts import (
    BudgetExceeded,
    ModelAdapter,
    ModelError,
    ModelRequest,
    ModelResponse,
    QuotaExceeded,
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
    tenant_model_calls_per_day: int | None = None  # organization-wide quota (UTC day)

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
        key, sha = store_bytes(s, self.store, self.tenant_id, data)
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

    @staticmethod
    def _request_payload(request: ModelRequest, sent_user: str) -> dict[str, Any]:
        return {
            "template_id": request.template_id,
            "system": request.system,
            "user": sent_user,
            "output_schema": request.output_schema,
            "max_output_tokens": request.max_output_tokens,
            "params": request.params,
        }

    def reserve_model_call(
        self,
        *,
        step: str,
        request: ModelRequest,
        sent_user: str,
        redaction: dict[str, Any],
        provider: str,
        requested_model: str | None,
        hosted: bool,
        cost_estimate: Decimal | None,
        ceiling: Decimal | None,
        daily_quota: int | None,
    ) -> str:
        """Reserve one call before it is sent. Under the organization's spend lock, refuse when
        the daily call quota or the spend ceiling would be exceeded; otherwise record the call
        now as `pending` with its estimated cost. A provider may charge for a call that never
        reports back, so a reservation is settled but never released: a worker that dies
        mid-call leaves the estimate charged."""
        with self.db.session(DbContext(tenant_id=self.tenant_id)) as s:
            s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"model-spend:{self.tenant_id}"},
            )
            if daily_quota is not None and _calls_today(s) >= daily_quota:
                raise QuotaExceeded(
                    f"the organization's daily model-call quota ({daily_quota}) is used up; "
                    "rules-only output is used"
                )
            if cost_estimate is not None and ceiling is not None:
                spent = Decimal(
                    str(
                        s.execute(
                            select(func.coalesce(func.sum(ModelCall.cost_estimate_usd), 0)).where(
                                ModelCall.origin == "live"
                            )
                        ).scalar_one()
                        or 0
                    )
                )
                if spent + cost_estimate > ceiling:
                    raise BudgetExceeded(
                        f"estimated cost {cost_estimate} USD would exceed the live spend ceiling "
                        f"{ceiling} USD"
                    )
            req_payload = self._request_payload(request, sent_user)
            self._artifact(s, "model_request", req_payload)
            row = ModelCall(
                tenant_id=self.tenant_id,
                run_id=self.run_id,
                step_name=step,
                provider=provider,
                requested_model=requested_model,
                prompt_template_id=request.template_id,
                prompt_template_digest=request.template_digest,
                input_digest=digest(req_payload),
                params={
                    "requested": {"max_output_tokens": request.max_output_tokens, **request.params},
                    "provider_defaults": "unknown unless reported by the provider",
                },
                cost_estimate_usd=cost_estimate,
                latency_ms=0,
                outcome="pending",
                origin="live" if hosted else "mocked",
                redaction=redaction,
            )
            s.add(row)
            s.flush()
            self.model_call_ids.append(str(row.id))
            return str(row.id)

    def settle_model_call(
        self,
        call_id: str,
        *,
        response: ModelResponse | None,
        outcome: str,
        error: str | None,
        cost: Decimal | None,
        latency_ms: int,
        validation: dict[str, Any] | None = None,
    ) -> None:
        """Complete a reservation with what happened. Without a response the provider may
        still have charged, so `cost` stays the estimate."""
        with self.db.session(DbContext(tenant_id=self.tenant_id)) as s:
            row = s.get(ModelCall, uuid.UUID(call_id))
            assert row is not None
            if response is not None:
                self._artifact(s, "model_response", {"text": response.text})
                row.resolved_model = response.resolved_model
                row.endpoint = response.endpoint
                row.sdk_version = response.sdk_version
                row.output_digest = digest({"text": response.text})
                row.input_tokens = response.input_tokens
                row.output_tokens = response.output_tokens
                row.origin = response.origin
            row.params = {
                **row.params,
                "effective": response.effective_params if response else None,
                "validation": validation or {},
            }
            row.cost_estimate_usd = cost
            row.latency_ms = latency_ms
            row.outcome = outcome
            row.error = error

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
        """Record a call in one step: used for calls refused before anything was sent."""
        with self.db.session(DbContext(tenant_id=self.tenant_id)) as s:
            req_payload = self._request_payload(request, sent_user)
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

    def model_calls_today(self) -> int:
        """Model calls counted against this organization's daily quota (see _calls_today)."""
        with self.db.session(DbContext(tenant_id=self.tenant_id)) as s:
            return _calls_today(s)

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


def call_model(
    adapter: ModelAdapter,
    request: ModelRequest,
    *,
    step: str,
    recorder: Recorder,
    budget: RunBudget,
    settings: Settings,
    validate: Callable[[str], tuple[Any, list[str]]],
    keep: Sequence[str] = (),
) -> tuple[Any | None, list[str], str]:
    """Run one bounded model call with redaction, budget, cost ceiling, recording and validation.

    `keep`: the case's own identifiers, left intact by redaction before a hosted call.

    Returns (validated_output or None, problems, model_call_id). Never raises ModelError:
    failures are recorded and returned as problems so the caller can fall back.
    """
    caps = adapter.capabilities
    sent_user = request.user
    redaction: dict[str, Any] = {"applied": False}
    if caps.hosted:
        rr = redact(request.user, keep)
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
    with tracing.span(
        f"model {step}",
        attributes={
            "gen_ai.system": caps.provider,
            "opsbridge.prompt_template": request.template_id,
            "opsbridge.hosted": caps.hosted,
        },
        step=step,
    ) as sp:
        value, problems, cid, outcome, response, cost = _call(
            adapter, req, sent_user, redaction, step, recorder, budget, settings, validate, start
        )
        sp.set_attribute("opsbridge.model.outcome", outcome)
        if response is not None and response.input_tokens is not None:
            sp.set_attribute("gen_ai.usage.input_tokens", response.input_tokens)
        if response is not None and response.output_tokens is not None:
            sp.set_attribute("gen_ai.usage.output_tokens", response.output_tokens)
    metrics.MODEL_CALLS.labels(caps.provider, step, outcome).inc()
    metrics.MODEL_DURATION.labels(caps.provider).observe(time.monotonic() - start)
    if response is not None:
        if response.input_tokens:
            metrics.MODEL_TOKENS.labels(caps.provider, "input").inc(response.input_tokens)
        if response.output_tokens:
            metrics.MODEL_TOKENS.labels(caps.provider, "output").inc(response.output_tokens)
    if cost:
        metrics.MODEL_COST.labels(caps.provider).inc(float(cost))
    return value, problems, cid


def _call(
    adapter: ModelAdapter,
    req: ModelRequest,
    sent_user: str,
    redaction: dict[str, Any],
    step: str,
    recorder: Recorder,
    budget: RunBudget,
    settings: Settings,
    validate: Callable[[str], tuple[Any, list[str]]],
    start: float,
) -> tuple[Any | None, list[str], str, str, ModelResponse | None, Decimal | None]:
    caps = adapter.capabilities
    cost: Decimal | None = None
    call_id: str | None = None
    try:
        budget.take_model_call()
        if caps.hosted:
            cost = _estimate_cost(req, settings)
        call_id = recorder.reserve_model_call(
            step=step,
            request=req,
            sent_user=sent_user,
            redaction=redaction,
            provider=caps.provider,
            requested_model=getattr(adapter, "model", None),
            hosted=caps.hosted,
            cost_estimate=cost,
            ceiling=Decimal(settings.live_spend_ceiling_usd or "0") if caps.hosted else None,
            daily_quota=budget.tenant_model_calls_per_day,
        )
        response = adapter.generate(req)
    except ModelError as exc:
        latency = int((time.monotonic() - start) * 1000)
        if call_id is None:  # refused before anything was sent: not charged
            call_id = recorder.model_call(
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
                latency_ms=latency,
            )
        else:  # sent, or possibly sent: the estimate stays charged
            recorder.settle_model_call(
                call_id, response=None, outcome=exc.category, error=str(exc), cost=cost,
                latency_ms=latency,
            )  # fmt: skip
        return None, [f"{exc.category}: {exc}"], call_id, exc.category, None, cost
    if len(response.text) > req.max_output_tokens * 8:
        problems = ["output exceeds the size bound"]
        value = None
    else:
        value, problems = validate(response.text)
    outcome = "ok" if not problems else "rejected_output"
    settled = _settled_cost(response, settings) if caps.hosted else None
    recorder.settle_model_call(
        call_id,
        response=response,
        outcome=outcome,
        error="; ".join(problems) or None,
        cost=settled if settled is not None else cost,
        latency_ms=response.latency_ms,
        validation={"accepted": not problems, "problems": problems},
    )
    return (value if not problems else None), problems, call_id, outcome, response, cost


# Outcomes of calls refused before anything was sent; they do not count against the quota.
NOT_SENT = ("quota_exceeded", "budget_exceeded", "routing_blocked")


def _calls_today(s: Any) -> int:
    """Calls sent or reserved for this organization since 00:00 UTC (database clock), any
    outcome except refusals; pending reservations count."""
    start = func.date_trunc("day", func.now(), "UTC")
    return int(
        s.execute(
            select(func.count())
            .select_from(ModelCall)
            .where(ModelCall.created_at >= start, ModelCall.outcome.not_in(NOT_SENT))
        ).scalar_one()
    )


def _prices(settings: Settings) -> tuple[Decimal, Decimal]:
    if not (
        settings.anthropic_price_input_per_mtok_usd and settings.anthropic_price_output_per_mtok_usd
    ):
        raise RoutingBlocked("prices not configured")
    return (
        Decimal(settings.anthropic_price_input_per_mtok_usd),
        Decimal(settings.anthropic_price_output_per_mtok_usd),
    )


def _settled_cost(response: ModelResponse, settings: Settings) -> Decimal | None:
    """Cost from the usage the provider reported; None if it reported none."""
    if response.input_tokens is None or response.output_tokens is None:
        return None
    price_in, price_out = _prices(settings)
    return (
        Decimal(response.input_tokens) * price_in + Decimal(response.output_tokens) * price_out
    ) / Decimal(1_000_000)


def _estimate_cost(req: ModelRequest, settings: Settings) -> Decimal:
    """The amount reserved before a call: input counted at 3 characters per token (system,
    user text and output schema; real tokenizers usually need fewer) plus the whole output
    allowance. An estimate with a margin, not a guaranteed bound; the reservation is settled
    from the usage the provider reports."""
    price_in, price_out = _prices(settings)
    chars = len(req.system) + len(req.user) + len(json.dumps(req.output_schema))
    in_tokens = Decimal(chars) / 3
    out_tokens = Decimal(req.max_output_tokens)
    return (in_tokens * price_in + out_tokens * price_out) / Decimal(1_000_000)
