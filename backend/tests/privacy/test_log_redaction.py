"""Logs must not carry credentials or common identifiers (OB-14)."""

from __future__ import annotations

import io
import logging
import logging.config
import random
import uuid
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


def test_uuids_are_never_altered_even_when_their_groups_look_like_numbers() -> None:
    # Hosted CI run 39207047346 failed when a case id ended in a 12-digit group that the phone
    # pattern redacted inside a trace attribute.
    for uid in (
        "2760c078-1f10-4f77-983b-978115788294",
        "12345678-1234-4234-9234-123456789012",
        "41111111-1111-4111-8111-111111111111",
    ):
        assert scrub(f"case {uid} dispatched") == f"case {uid} dispatched"
    rng = random.Random(20261019)
    for _ in range(20_000):
        uid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        assert scrub(uid) == uid


def test_identifiers_next_to_a_uuid_are_still_redacted() -> None:
    uid = "2760c078-1f10-4f77-983b-978115788294"
    out = scrub(
        f"{uid} call +1 415 555 0134 or ap@acme-furniture.example; card 4111 1111 1111 1111"
    )
    assert uid in out
    assert "555 0134" not in out and "ap@acme" not in out and "4111 1111" not in out


def test_hyphen_joined_ids_are_kept_and_phone_numbers_are_not() -> None:
    # Task references (T-1705292356) and long business numbers (INV-100023456) are ids; the
    # simulator's random task reference was redacted as a phone number in a trace attribute.
    for ident in ("T-1705292356", "INV-100023456", "PO-4500012345", "GRN-123456789"):
        assert scrub(f"created {ident}") == f"created {ident}"
    for phone in ("call 4155550134", "call +1 415 555 0134", "call (415) 555-0134 today"):
        assert "[REDACTED:phone]" in scrub(phone), phone


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
