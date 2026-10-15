"""Logs must not carry credentials or common identifiers (OB-14)."""

from __future__ import annotations

import io
import logging
import logging.config
from collections.abc import Iterator

import pytest

from opsbridge.telemetry.logging import RedactingFilter, scrub, uvicorn_log_config

# Synthetic look-alikes only; none of these are real credentials.
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJlLWJ5dGVz"
CODE = "f3a9c1d2-synthetic-auth-code"


@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        (f"GET /api/v1/auth/callback?state=abc123state&code={CODE}", CODE),
        (f"GET /api/v1/auth/callback?state=abc123state&code={CODE}", "abc123state"),
        (f"Authorization: Bearer {JWT}", JWT),
        (f"delegation header carried {JWT}", JWT),
        ("client_secret=s3cr3t-value-for-tests", "s3cr3t-value-for-tests"),
        ('{"password": "hunter2-synthetic"}', "hunter2-synthetic"),
        ("token ghp_" + "a" * 36 + " rejected", "ghp_" + "a" * 36),
        ("key sk-ant-" + "b" * 24, "sk-ant-" + "b" * 24),
        ("contact ap@acme-furniture.example for remittance", "ap@acme-furniture.example"),
        ("remit to DE89 3704 0044 0532 0130 00", "DE89 3704 0044 0532 0130 00"),
    ],
)
def test_scrub_removes_credentials_and_identifiers(raw: str, leak: str) -> None:
    assert leak not in scrub(raw)


def test_scrub_keeps_operational_detail() -> None:
    line = (
        "job investigate 6fbd676f-4291-4c1f-9679-8a7bf3236090 failed (retry): HTTP 503 after 2.0s"
    )
    assert scrub(line) == line


@pytest.fixture
def uvicorn_logging() -> Iterator[tuple[io.StringIO, io.StringIO]]:
    """Applies the real uvicorn config (as the API/MCP processes do) with captured streams."""
    out, err = io.StringIO(), io.StringIO()
    cfg = uvicorn_log_config("info")
    cfg["handlers"]["access"]["stream"] = out
    cfg["handlers"]["default"]["stream"] = err
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    logging.config.dictConfig(cfg)
    yield out, err
    root.handlers[:], root.level = saved[0], saved[1]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()


def test_uvicorn_access_log_keeps_its_format_without_the_oidc_code(uvicorn_logging) -> None:  # type: ignore[no-untyped-def]
    out, _ = uvicorn_logging
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:50000",
        "GET",
        f"/api/v1/auth/callback?code={CODE}&state=s1",
        "1.1",
        302,
    )
    line = out.getvalue()
    assert CODE not in line and "s1" not in line
    assert '"GET /api/v1/auth/callback?code=[REDACTED]&state=[REDACTED] HTTP/1.1" 302' in line


def test_application_logs_and_tracebacks_are_scrubbed(uvicorn_logging) -> None:  # type: ignore[no-untyped-def]
    _, err = uvicorn_logging
    log = logging.getLogger("opsbridge.api")
    log.warning("identity provider said %s", f"Bearer {JWT}")
    try:
        raise RuntimeError("upstream echoed password=hunter2-synthetic for ap@acme.example")
    except RuntimeError:
        log.exception("request failed")
    text = err.getvalue()
    assert "request failed" in text and "RuntimeError" in text
    for leak in (JWT, "hunter2-synthetic", "ap@acme.example"):
        assert leak not in text


def test_unformattable_record_is_withheld_not_dropped() -> None:
    record = logging.LogRecord(
        "opsbridge.x", logging.INFO, __file__, 1, "%d items for %s", ("x",), None
    )
    assert RedactingFilter().filter(record)
    assert record.args is None and "withheld" in record.getMessage()
