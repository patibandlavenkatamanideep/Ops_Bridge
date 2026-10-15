"""OpenTelemetry tracing with safe identifiers only.

Spans carry identifiers (tenant, case, run, operation, attempt, remote reference, correlation
id), route templates, statuses and durations — never document text, prompts, model output,
tool payloads or credentials. Attribute strings pass through the log scrubber as a second
line of defence. Without `configure_tracing` (or a provider set by tests) spans are no-ops.

Exporters: OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` (or traces endpoint) is set; a local
JSON-lines file when `OPSBRIDGE_TRACE_FILE` is set; otherwise nothing leaves the process.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from opsbridge import __version__

from .logging import scrub

tracer = trace.get_tracer("opsbridge", __version__)

ID_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "job_id",
    "operation_id",
    "attempt_id",
    "remote_ref",
    "correlation_id",
    "step",
)


class JsonlSpanExporter(SpanExporter):
    """Append finished spans to a local file (diagnostics without a collector)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = []
        for sp in spans:
            ctx = sp.get_span_context()
            parent = sp.parent
            lines.append(
                json.dumps(
                    {
                        "name": sp.name,
                        "trace_id": format(ctx.trace_id, "032x") if ctx else None,
                        "span_id": format(ctx.span_id, "016x") if ctx else None,
                        "parent_span_id": format(parent.span_id, "016x") if parent else None,
                        "start": sp.start_time,
                        "end": sp.end_time,
                        "status": sp.status.status_code.name,
                        "attributes": dict(sp.attributes or {}),
                        "service": (sp.resource.attributes or {}).get("service.name"),
                    },
                    default=str,
                )
            )
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def configure_tracing(service: str) -> TracerProvider | None:
    """Install a tracer provider for this process if an exporter is configured."""
    otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    )
    trace_file = os.environ.get("OPSBRIDGE_TRACE_FILE")
    if not otlp and not trace_file:
        return None
    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "service.version": __version__})
    )
    if otlp:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    if trace_file:
        provider.add_span_processor(BatchSpanProcessor(JsonlSpanExporter(Path(trace_file))))
    trace.set_tracer_provider(provider)
    return provider


def _safe(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    return scrub(str(value))[:200]


def set_ids(span: Span | None = None, **ids: Any) -> None:
    """Attach correlation identifiers (opsbridge.<name>) to a span; None values are skipped."""
    target = span or trace.get_current_span()
    for name, value in ids.items():
        if value is not None:
            target.set_attribute(f"opsbridge.{name}", _safe(value))


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    context: Context | None = None,
    attributes: Mapping[str, Any] | None = None,
    **ids: Any,
) -> Iterator[Span]:
    """Start a span as current; record an exception type (not its text) as the error."""
    with tracer.start_as_current_span(
        name, kind=kind, context=context, record_exception=False, set_status_on_exception=False
    ) as sp:
        set_ids(sp, **ids)
        for key, value in (attributes or {}).items():
            if value is not None:
                sp.set_attribute(key, _safe(value))
        try:
            yield sp
        except BaseException as exc:
            sp.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            sp.set_attribute("error.type", type(exc).__name__)
            raise


def carrier() -> dict[str, str]:
    """W3C trace context of the current span, for handing work to another process."""
    out: dict[str, str] = {}
    propagate.inject(out)
    return out


def context_from(carrier_: Mapping[str, str] | None) -> Context | None:
    return propagate.extract(dict(carrier_)) if carrier_ else None


def current_ids() -> tuple[str | None, str | None]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
