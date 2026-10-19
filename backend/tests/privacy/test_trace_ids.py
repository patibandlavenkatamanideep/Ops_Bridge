"""Correlation ids on spans are kept exactly (they are system-generated, not personal data)
while credentials passed by mistake are still removed; other attributes get full scrubbing."""

from __future__ import annotations

from typing import Any

from opsbridge.telemetry.tracing import set_ids


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def test_ids_survive_exactly_and_credentials_do_not() -> None:
    span = _Span()
    set_ids(
        span,  # type: ignore[arg-type]
        case_id="2760c078-1f10-4f77-983b-978115788294",
        remote_ref="T-1705292356",
        attempt_id="12345678-1234-4234-9234-123456789012",
        correlation_id="Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln",  # a mistake
    )
    a = span.attributes
    assert a["opsbridge.case_id"] == "2760c078-1f10-4f77-983b-978115788294"
    assert a["opsbridge.remote_ref"] == "T-1705292356"
    assert a["opsbridge.attempt_id"] == "12345678-1234-4234-9234-123456789012"
    assert "eyJ" not in a["opsbridge.correlation_id"]
    assert all(len(str(v)) <= 200 for v in a.values())
