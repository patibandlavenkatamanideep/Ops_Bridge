"""Application-level model contract shared by every adapter.

Adapters declare capabilities explicitly. A request parameter an adapter does not
support is rejected with `UnsupportedParameter` — never silently dropped — so the
recorded configuration is what was actually sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Origin = Literal["live", "recorded", "mocked"]


class ModelError(Exception):
    """Base class; `category` is recorded with the model call."""

    category = "error"


class UnsupportedParameter(ModelError):
    category = "unsupported_parameter"


class ModelUnavailable(ModelError):
    category = "unavailable"


class ModelTimeout(ModelError):
    category = "timeout"


class ModelRefused(ModelError):
    """The provider declined or stopped before producing usable output."""

    category = "refused"


class BudgetExceeded(ModelError):
    category = "budget_exceeded"


class RoutingBlocked(ModelError):
    """Data-routing policy or entitlements forbid this inference path."""

    category = "routing_blocked"


@dataclass(frozen=True)
class Capabilities:
    provider: str
    structured_output: Literal["native_schema", "none"]
    supported_params: frozenset[str]
    hosted: bool
    local: bool
    reports_token_usage: bool
    reports_resolved_model: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "structured_output": self.structured_output,
            "supported_params": sorted(self.supported_params),
            "hosted": self.hosted,
            "local": self.local,
            "reports_token_usage": self.reports_token_usage,
            "reports_resolved_model": self.reports_resolved_model,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ModelRequest:
    template_id: str
    template_digest: str
    system: str
    user: str
    output_schema: dict[str, Any]
    max_output_tokens: int
    timeout_seconds: float
    params: dict[str, Any] = field(default_factory=dict)  # provider-specific, validated per adapter


@dataclass(frozen=True)
class ModelResponse:
    text: str
    provider: str
    requested_model: str | None
    resolved_model: str | None
    origin: Origin
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    stop_reason: str | None
    effective_params: dict[str, Any]
    endpoint: str | None
    sdk_version: str | None


class ModelAdapter(Protocol):
    name: str
    capabilities: Capabilities

    def generate(self, request: ModelRequest) -> ModelResponse: ...


def check_params(request: ModelRequest, capabilities: Capabilities) -> None:
    unsupported = sorted(set(request.params) - capabilities.supported_params)
    if unsupported:
        raise UnsupportedParameter(
            f"{capabilities.provider} does not support parameter(s) {unsupported}; "
            "they were not sent (refusing rather than dropping them)"
        )
