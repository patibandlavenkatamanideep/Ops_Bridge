"""Bounded HTTP reads (production review A04).

Before, connectors read a whole response and then compared its length with the limit: the
limit protected parsing, not memory. A 7 MiB stream against a 5 MiB limit was consumed in
full. Reads now stop as soon as the decoded body passes the limit, whatever the
Content-Length says or however well the body compresses, and a body that trickles in slowly
stops at a total deadline the per-read timeout never reaches.

A compressed body is accepted only when its stream reached its own end: one that stops short
(even after a complete-looking document), fails its checksum, or carries anything after its
end (a second gzip member included) is a decoding error, never a body.
"""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from opsbridge.connectors import http_transport as ht
from opsbridge.connectors.github import GitHubIssuesConnector
from opsbridge.connectors.purchasing import PurchasingClient
from opsbridge.connectors.source_contract import SourceRecordInvalid, SourceUnavailable
from opsbridge.connectors.tasks import (
    AmbiguousFailure,
    LookupUnavailable,
    SimTaskConnector,
    TaskRequest,
)

MiB = 1024 * 1024
CHUNK = 64 * 1024


class Body:
    """A streamed response body that records how much of it was read."""

    def __init__(self, total: int, chunk: bytes = b"x" * CHUNK) -> None:
        self.total, self.chunk, self.sent = total, chunk, 0

    def __iter__(self) -> Iterator[bytes]:
        while self.sent < self.total:
            piece = self.chunk[: self.total - self.sent]
            self.sent += len(piece)
            yield piece


def streamed(data: bytes) -> Iterator[bytes]:
    """`data` as a network body arrives: in chunks (bytes given to httpx are read eagerly)."""
    for i in range(0, len(data), CHUNK):
        yield data[i : i + CHUNK]


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="http://source", transport=httpx.MockTransport(handler))


def test_a_stream_past_the_limit_is_stopped_before_it_is_consumed() -> None:
    body = Body(7 * MiB)
    client = _client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ht.ResponseTooLarge):
        ht.request(client, "GET", "/big", max_bytes=5 * MiB, total_seconds=30)
    assert body.sent <= 5 * MiB + CHUNK  # not the whole 7 MiB


def test_a_content_length_that_lies_does_not_help() -> None:
    body = Body(7 * MiB)
    client = _client(
        lambda request: httpx.Response(200, headers={"content-length": "100"}, content=body)
    )
    with pytest.raises(ht.ResponseTooLarge):
        ht.request(client, "GET", "/liar", max_bytes=5 * MiB, total_seconds=30)
    assert body.sent <= 5 * MiB + CHUNK


def test_a_declared_length_past_the_limit_is_refused_before_reading() -> None:
    body = Body(7 * MiB)
    client = _client(
        lambda request: httpx.Response(200, headers={"content-length": str(7 * MiB)}, content=body)
    )
    with pytest.raises(ht.ResponseTooLarge):
        ht.request(client, "GET", "/declared", max_bytes=5 * MiB, total_seconds=30)
    assert body.sent <= CHUNK


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_a_body_that_inflates_past_the_limit_is_stopped(encoding: str) -> None:
    packed = (
        gzip.compress(b"\0" * 64 * MiB) if encoding == "gzip" else zlib.compress(b"\0" * 64 * MiB)
    )
    assert len(packed) < 1 * MiB  # small on the wire
    client = _client(
        lambda request: httpx.Response(
            200, headers={"content-encoding": encoding}, content=streamed(packed)
        )
    )
    with pytest.raises(ht.ResponseTooLarge):
        ht.request(client, "GET", "/bomb", max_bytes=5 * MiB, total_seconds=30)


def test_an_ordinary_compressed_body_reads_back_exactly() -> None:
    doc = {"items": [{"key": f"GR-{i}"} for i in range(1000)]}
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=streamed(gzip.compress(json.dumps(doc).encode())),
        )
    )
    r = ht.request(client, "GET", "/ok", max_bytes=5 * MiB, total_seconds=30)
    assert r.status_code == 200 and json.loads(r.content) == doc


def test_an_encoding_it_cannot_bound_is_refused() -> None:
    client = _client(
        lambda request: httpx.Response(
            200, headers={"content-encoding": "br"}, content=streamed(b"...")
        )
    )
    with pytest.raises(ht.UnsupportedEncoding):
        ht.request(client, "GET", "/br", max_bytes=5 * MiB, total_seconds=30)


def test_a_slow_drip_stops_at_the_total_deadline() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 1.0  # each chunk arrives a second after the last, inside any read timeout
        return now[0]

    body = Body(1 * MiB, chunk=b"x" * 1024)
    client = _client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ht.ReadDeadlineExceeded):
        ht.request(client, "GET", "/drip", max_bytes=5 * MiB, total_seconds=10, clock=clock)
    assert body.sent < 1 * MiB


def test_the_purchasing_source_stops_an_oversized_record_early() -> None:
    body = Body(7 * MiB)
    source = PurchasingClient(
        "http://sim",
        "secret",
        "org",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)),
    )
    with pytest.raises(SourceRecordInvalid, match="too large"):
        source.fetch("invoices", "INV-1")
    assert body.sent <= 5 * MiB + CHUNK


def test_a_purchasing_read_that_never_finishes_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 1.0
        return now[0]

    monkeypatch.setattr(ht, "_monotonic", clock)
    body = Body(1 * MiB, chunk=b"x" * 1024)
    source = PurchasingClient(
        "http://sim", "secret", "org", timeout=2.0,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)),
    )  # fmt: skip
    with pytest.raises(SourceUnavailable):
        source.fetch("invoices", "INV-1")


@pytest.mark.parametrize(
    "reply",
    [b"{not json", b'{"no_id": true}', b"x" * (2 * MiB)],
    ids=["malformed", "missing_id", "oversized"],
)
def test_an_unreadable_reply_to_a_create_is_ambiguous(reply: bytes) -> None:
    # The tracker answered 201: the task may well exist, so this is not a definite failure.
    tracker = SimTaskConnector(
        "http://sim",
        "secret",
        "org",
        idempotency_keys=False,
        lookup="strong",
        transport=httpx.MockTransport(lambda r: httpx.Response(201, content=reply)),
    )
    with pytest.raises(AmbiguousFailure):
        tracker.create(TaskRequest("t", "b", None, "m"), None, 2.0)


TASK = json.dumps({"id": "T-1", "marker": "m", "title": "t"}).encode()
LISTING = json.dumps({"items": [json.loads(TASK)]}).encode()
ISSUE = json.dumps({"number": 7, "html_url": "https://github.test/o/r/issues/7"}).encode()


def compress(encoding: str, data: bytes) -> bytes:
    return gzip.compress(data) if encoding == "gzip" else zlib.compress(data)


def damaged(encoding: str, data: bytes, damage: str) -> bytes:
    packed = compress(encoding, data)
    if damage == "trailer_missing":  # the whole document decodes; the checksum never arrives
        return packed[:-4]
    if damage == "cut_mid_stream":
        return packed[: len(packed) // 2]
    if damage == "corrupt":
        middle = len(packed) // 2
        return packed[:middle] + bytes([packed[middle] ^ 0xFF]) + packed[middle + 1 :]
    if damage == "trailing_bytes":
        return packed + b"junk"
    assert damage == "second_member"
    return packed + compress(encoding, data)


DAMAGE = ["trailer_missing", "cut_mid_stream", "corrupt", "trailing_bytes", "second_member"]


def _encoded(status: int, encoding: str, raw: bytes) -> Any:
    return lambda request: httpx.Response(
        status, headers={"content-encoding": encoding}, content=streamed(raw)
    )


@pytest.mark.parametrize("damage", DAMAGE)
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_a_damaged_compressed_body_is_a_decoding_error(encoding: str, damage: str) -> None:
    client = _client(_encoded(200, encoding, damaged(encoding, TASK, damage)))
    with pytest.raises(ht.CorruptEncoding):
        ht.request(client, "GET", "/damaged", max_bytes=5 * MiB, total_seconds=30)
    assert issubclass(ht.CorruptEncoding, httpx.DecodingError)  # the callers' existing contract


def test_a_compressed_body_with_no_bytes_is_empty() -> None:
    # 204 and 304 replies carry no body whatever their headers say; there is no stream to check.
    client = _client(lambda request: httpx.Response(204, headers={"content-encoding": "gzip"}))
    r = ht.request(client, "DELETE", "/x", max_bytes=5 * MiB, total_seconds=30)
    assert r.status_code == 204 and r.content == b""


@pytest.mark.parametrize("damage", DAMAGE)
def test_an_unreadable_compressed_reply_to_a_tracker_create_is_ambiguous(damage: str) -> None:
    # The tracker answered 201; a reply that cannot be fully decoded proves nothing either way.
    tracker = SimTaskConnector(
        "http://sim", "secret", "org", idempotency_keys=False, lookup="strong",
        transport=httpx.MockTransport(_encoded(201, "gzip", damaged("gzip", TASK, damage))),
    )  # fmt: skip
    with pytest.raises(AmbiguousFailure):
        tracker.create(TaskRequest("t", "b", None, "m"), None, 2.0)


@pytest.mark.parametrize("damage", DAMAGE)
def test_an_unreadable_compressed_reply_to_an_issue_create_is_ambiguous(damage: str) -> None:
    github = GitHubIssuesConnector(
        "o/r", "token", allowlist=frozenset({"o/r"}), writes_authorized=True,
        base_url="http://github.test",
        transport=httpx.MockTransport(_encoded(201, "gzip", damaged("gzip", ISSUE, damage))),
    )  # fmt: skip
    with pytest.raises(AmbiguousFailure):
        github.create(TaskRequest("t", "b", None, "m"), None, 2.0)


def test_an_unreadable_compressed_read_back_proves_nothing() -> None:
    # A truncated read-back is not "the task is absent" and not "the task is there".
    def handler(request: httpx.Request) -> httpx.Response:
        doc = TASK if request.url.path.endswith("/T-1") else LISTING
        return _encoded(200, "gzip", damaged("gzip", doc, "trailer_missing"))(request)

    tracker = SimTaskConnector(
        "http://sim", "secret", "org", idempotency_keys=False, lookup="strong",
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    with pytest.raises(LookupUnavailable):
        tracker.get("T-1", 2.0)
    with pytest.raises(LookupUnavailable):
        tracker.find_by_marker("m", 2.0)


def test_an_unreadable_compressed_source_record_is_not_read() -> None:
    record = json.dumps({"invoiceId": "INV-1"}).encode()
    source = PurchasingClient(
        "http://sim", "secret", "org",
        transport=httpx.MockTransport(
            _encoded(200, "gzip", damaged("gzip", record, "trailer_missing"))
        ),
    )  # fmt: skip
    with pytest.raises(SourceUnavailable):
        source.fetch("invoices", "INV-1")
