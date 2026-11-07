"""Bounded HTTP reads (production review A04).

Before, connectors read a whole response and then compared its length with the limit: the
limit protected parsing, not memory. A 7 MiB stream against a 5 MiB limit was consumed in
full. Reads now stop as soon as the decoded body passes the limit, whatever the
Content-Length says or however well the body compresses, and a body that trickles in slowly
stops at a total deadline the per-read timeout never reaches.

A compressed body is accepted only when its stream reached its own end: one that stops short
(even after a complete-looking document), fails its checksum, or carries anything after its
end (a second gzip member included) is a decoding error, never a body.

One deadline covers the whole exchange (review N07). It was checked only between body
chunks: connecting and the headers were bounded by the per-read timeout alone (headers sent a
byte at a time never ended), a read that blocked waited its full timeout however little of
the total was left, and a reply whose headers or end arrived late with no chunk after them
was accepted. Now every socket wait is capped at the time left, and the deadline is checked
after the headers and at the end of the body. Fake clocks test the checks; a scripted
loopback server tests the waits in wall time. Looking a name up and trying each of its
addresses count against the same deadline: a resolver that did not answer delayed a request
by its own timeout, and each address tried was given all the time left.
"""

from __future__ import annotations

import gzip
import json
import socket
import ssl
import time
import zlib
from collections.abc import Callable, Iterator
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


# --- one deadline over every phase (review N07) ----------------------------------------

from .scripted_server import OK_HEADERS, scripted, trickle  # noqa: E402


def stepping(*times: float) -> Callable[[], float]:
    """A clock answering `times` in turn, then the last one again."""
    seen = list(times)

    def clock() -> float:
        return seen.pop(0) if len(seen) > 1 else seen[0]

    return clock


def test_a_reply_whose_headers_arrive_after_the_deadline_is_refused_even_when_empty() -> None:
    client = _client(lambda request: httpx.Response(200, content=iter([])))
    with pytest.raises(ht.ReadDeadlineExceeded):
        ht.request(client, "GET", "/late", max_bytes=MiB, total_seconds=10, clock=stepping(0, 11))


def test_a_body_that_ends_after_the_deadline_is_refused() -> None:
    client = _client(lambda request: httpx.Response(200, content=Body(1024, chunk=b"x" * 1024)))
    clock = stepping(0, 1, 1, 11)  # started; the headers; the one chunk; the end, too late
    with pytest.raises(ht.ReadDeadlineExceeded):
        ht.request(client, "GET", "/late-end", max_bytes=MiB, total_seconds=10, clock=clock)
    on_time = _client(lambda request: httpx.Response(200, content=Body(1024, chunk=b"x" * 1024)))
    r = ht.request(on_time, "GET", "/", max_bytes=MiB, total_seconds=10, clock=stepping(0, 1, 2, 3))
    assert len(r.content) == 1024


class _Recording:
    """Network backend and stream doubles that record the timeouts they are given."""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, *_: object
    ) -> _Recording:
        self.timeouts.append(timeout)
        return self

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.timeouts.append(timeout)
        return b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.timeouts.append(timeout)

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _Recording:
        self.timeouts.append(timeout)
        return self

    def close(self) -> None:
        pass


def test_every_socket_wait_is_capped_at_the_time_left() -> None:
    inner = _Recording()
    backend = ht.DeadlineBackend(inner)  # type: ignore[arg-type]
    with ht.deadline(0.25):
        stream = backend.connect_tcp("192.0.2.10", 443, timeout=30.0)
        tls = stream.start_tls(ssl.create_default_context(), "source", timeout=30.0)
        tls.write(b"GET / HTTP/1.1\r\n\r\n", timeout=30.0)
        tls.read(65536, timeout=30.0)
        tls.read(65536, timeout=None)  # no per-read timeout: the deadline still applies
    assert len(inner.timeouts) == 5
    assert all(t is not None and 0 < t <= 0.25 for t in inner.timeouts), inner.timeouts
    inner.timeouts.clear()
    backend.connect_tcp("192.0.2.10", 443, timeout=30.0).read(1, timeout=30.0)  # no request
    assert inner.timeouts == [30.0, 30.0]


CHUNKED = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"


@pytest.mark.parametrize(
    "steps",
    [
        [(3.0, OK_HEADERS + b"{}")],
        trickle(OK_HEADERS, 0.1),  # each byte inside the per-read timeout; ~7 s in all
        [(0.0, CHUNKED + b"1\r\n{\r\n"), (3.0, b"1\r\n}\r\n0\r\n\r\n")],
    ],
    ids=["delayed headers", "trickled headers", "a blocked body read"],
)
def test_every_phase_counts_against_one_total_deadline(steps: list[tuple[float, bytes]]) -> None:
    with scripted(steps) as script:
        client = httpx.Client(base_url=script.url, timeout=30.0, transport=ht.deadline_transport())
        started = time.monotonic()
        with client, pytest.raises(httpx.TimeoutException):
            ht.request(client, "GET", "/", max_bytes=MiB, total_seconds=0.5)
        assert time.monotonic() - started < 1.5
        assert script.client_closed.wait(2.0)  # the refused connection is not left open


def test_a_connector_built_without_a_transport_keeps_its_total_deadline() -> None:
    with scripted(trickle(OK_HEADERS + b"{}", 0.1)) as script:
        source = PurchasingClient(script.url, "secret", "org", timeout=0.2)  # 0.6 s in all
        started = time.monotonic()
        with pytest.raises(SourceUnavailable):
            source.fetch("invoices", "INV-1")
        source.close()
        assert time.monotonic() - started < 2.0


# --- looking the name up, and each address, within the deadline -------------------------

SOURCE = "source.test"  # a name no resolver knows; each test answers for it


def answering(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str], port: int, *, after: float = 0.0
) -> None:
    """Looking up SOURCE takes `after` seconds and gives `addresses`; other names as usual."""
    real = socket.getaddrinfo

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != SOURCE:
            return real(host, *args, **kwargs)
        time.sleep(after)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_a_resolver_that_does_not_answer_counts_against_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with scripted([(0.0, OK_HEADERS + b"{}")]) as script:
        answering(monkeypatch, ["127.0.0.1"], script.port, after=3.0)
        client = httpx.Client(
            base_url=f"http://{SOURCE}:{script.port}", timeout=30.0,
            transport=ht.deadline_transport(),
        )  # fmt: skip
        started = time.monotonic()
        with client, pytest.raises(httpx.ConnectTimeout):
            ht.request(client, "GET", "/", max_bytes=MiB, total_seconds=0.5)
        assert time.monotonic() - started < 1.5


def test_a_name_looked_up_in_time_connects_past_an_address_that_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> None:
        if address[0] == "192.0.2.1":
            raise ConnectionRefusedError("refused")
        real(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    with scripted([(0.0, OK_HEADERS + b"{}")]) as script:
        answering(monkeypatch, ["192.0.2.1", "127.0.0.1"], script.port, after=0.05)
        client = httpx.Client(
            base_url=f"http://{SOURCE}:{script.port}", timeout=30.0,
            transport=ht.deadline_transport(),
        )  # fmt: skip
        with client:
            assert ht.request(client, "GET", "/", max_bytes=MiB, total_seconds=5).json() == {}


def test_each_address_tried_has_only_the_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[Any] = []

    def connect(sock: socket.socket, address: Any) -> None:
        tried.append(address)
        if address[0] == "192.0.2.1":  # refuses, late
            time.sleep(0.4)
            raise ConnectionRefusedError("refused")
        time.sleep(sock.gettimeout() or 0)  # as a SYN nobody answers
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket.socket, "connect", connect)
    answering(monkeypatch, ["192.0.2.1", "192.0.2.2", "192.0.2.3"], 8080)
    client = httpx.Client(
        base_url=f"http://{SOURCE}:8080", timeout=30.0, transport=ht.deadline_transport()
    )
    started = time.monotonic()
    with client, pytest.raises(httpx.ConnectTimeout):
        ht.request(client, "GET", "/", max_bytes=MiB, total_seconds=0.6)
    # Before: 0.4 s, then the whole 0.6 s again for each of the other two (1.6 s).
    assert time.monotonic() - started < 0.85, tried
    assert tried[:2] == [("192.0.2.1", 8080), ("192.0.2.2", 8080)]
