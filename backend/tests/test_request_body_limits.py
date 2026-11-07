"""Request bodies are bounded by the bytes actually received, and in time (review N02).

The perimeter refused a declared Content-Length over the limit and every chunked body, and
trusted the rest. A body framed neither way (HTTP/2 has no chunked coding and need not
declare a length) passed both checks: the application read it to its end, and FastAPI parsed
a multipart upload into a temporary file before the upload route compared its size. A body
could also arrive as slowly as a client liked. Now every received byte is counted, the
request is refused the moment the count passes the limit, a body that does not arrive in
time is refused, and chunked bodies within the limit are accepted.

These drive the application `create_app` builds with ASGI messages, which choose the framing
exactly, and once through uvicorn on a loopback socket.
"""

from __future__ import annotations

import json
import select
import socket
import time
from collections.abc import Sequence
from typing import Any

import anyio
import pytest
import sqlalchemy
from fastapi import FastAPI, File, Form, UploadFile
from starlette.types import Message

from opsbridge.api.app import create_app
from opsbridge.config import Settings
from opsbridge.evaluation.local_server import serve
from opsbridge.persistence.db import Database

LIMIT = 1000
SECONDS = 0.3


def make_app(**changes: Any) -> tuple[FastAPI, list[int]]:
    """The real middleware stack, with two routes that read a body and record each call."""
    limits = {"max_request_bytes": LIMIT, "max_request_body_seconds": SECONDS} | changes
    settings = Settings(_env_file=None, rate_limit_shared=False, **limits)
    app = create_app(settings=settings, db=Database(sqlalchemy.create_engine("sqlite://")))
    calls: list[int] = []

    @app.post("/api/v1/test/json")
    async def take_json(body: dict[str, str]) -> dict[str, int]:
        calls.append(len(body["x"]))
        return {"x": len(body["x"])}

    @app.post("/api/v1/test/upload")
    async def take_upload(kind: str = Form(...), file: UploadFile = File(...)) -> dict[str, int]:
        data = await file.read()
        calls.append(len(data))
        return {"size": len(data)}

    return app, calls


def json_body(size: int) -> bytes:
    """A JSON object of exactly `size` bytes."""
    body = b'{"x": "' + b"x" * (size - 9) + b'"}'
    assert len(body) == size
    return body


BOUNDARY = "b0undary"


def multipart(data: bytes, *, boundary: str = BOUNDARY) -> bytes:
    return (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="kind"\r\n\r\nattachment\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )


class Result:
    def __init__(self) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = b""
        self.taken = 0  # body messages the application asked for

    def json(self) -> Any:
        return json.loads(self.body)


async def _call(
    app: FastAPI,
    path: str,
    chunks: Sequence[bytes],
    headers: dict[str, str],
    *,
    gap: float = 0.0,
    then: str = "end",
) -> Result:
    """POST `chunks` as one request, `gap` seconds apart. After the last chunk: "end" (the
    body is complete), "hang" (it never completes) or "disconnect" (the client leaves)."""
    out = Result()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "2",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        if out.taken < len(chunks):
            if gap:
                await anyio.sleep(gap)
            out.taken += 1
            last = out.taken == len(chunks) and then == "end"
            return {"type": "http.request", "body": chunks[out.taken - 1], "more_body": not last}
        if then == "disconnect":
            return {"type": "http.disconnect"}
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            out.status = message["status"]
            out.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            out.body += message.get("body", b"")

    with anyio.fail_after(10):
        await app(scope, receive, send)
    return out


def call(app: FastAPI, path: str, chunks: Sequence[bytes], **kwargs: Any) -> Result:
    headers = kwargs.pop("headers", {}) | {"content-type": "application/json"}
    return anyio.run(lambda: _call(app, path, chunks, headers, **kwargs))


def pieces(body: bytes, size: int) -> list[bytes]:
    return [body[i : i + size] for i in range(0, len(body), size)]


def refused(r: Result, status: int, code: str) -> None:
    assert r.status == status, r.body
    error = r.json()["error"]
    assert error["code"] == code
    assert error["correlation_id"] == r.headers["x-correlation-id"]


@pytest.mark.parametrize(
    "headers",
    [{}, {"transfer-encoding": "chunked"}, {"content-length": "10"}],
    ids=["no length (HTTP/2)", "chunked", "a length smaller than the body"],
)
def test_a_body_over_the_limit_is_refused_as_soon_as_it_passes_it(
    headers: dict[str, str],
) -> None:
    app, calls = make_app()
    body = json_body(5 * LIMIT)
    r = call(app, "/api/v1/test/json", pieces(body, 50), headers=headers)
    refused(r, 413, "payload_too_large")
    assert calls == []
    assert r.taken == LIMIT // 50 + 1  # the chunk that passed the limit, and none after it


def test_the_limit_itself_is_accepted_and_one_byte_more_is_not() -> None:
    app, calls = make_app()
    r = call(app, "/api/v1/test/json", pieces(json_body(LIMIT), 7))  # many small chunks
    assert r.status == 200 and r.json() == {"x": LIMIT - 9}
    refused(
        call(app, "/api/v1/test/json", pieces(json_body(LIMIT + 1), 7)), 413, "payload_too_large"
    )
    # A declared length over the limit is refused before any of the body is read.
    r = call(app, "/api/v1/test/json", [json_body(LIMIT + 1)], headers={"content-length": "1001"})
    refused(r, 413, "payload_too_large")
    assert r.taken == 0 and calls == [LIMIT - 9]


def test_a_chunked_body_within_the_limit_is_accepted() -> None:
    """Chunked bodies were refused (411) because their size could not be checked before
    reading; now that every received byte is counted, they need not be."""
    app, calls = make_app()
    r = call(
        app,
        "/api/v1/test/json",
        pieces(json_body(300), 20),
        headers={"transfer-encoding": "chunked"},
    )
    assert r.status == 200 and calls == [291]


def test_an_upload_over_the_limit_is_refused_before_the_route_parses_it_whole() -> None:
    app, calls = make_app()
    body = multipart(b"y" * (20 * LIMIT))
    upload = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
    r = anyio.run(lambda: _call(app, "/api/v1/test/upload", pieces(body, 100), upload))
    refused(r, 413, "payload_too_large")
    assert calls == [] and r.taken == LIMIT // 100 + 1 < len(pieces(body, 100))


def test_an_ordinary_upload_is_read_exactly_and_a_malformed_one_is_a_client_error() -> None:
    app, calls = make_app()
    upload = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
    data = bytes(range(256)) * 2
    r = anyio.run(lambda: _call(app, "/api/v1/test/upload", pieces(multipart(data), 64), upload))
    assert r.status == 200 and r.json() == {"size": 512} and calls == [512]
    broken = multipart(data)[:-30]  # the closing boundary never comes
    r = anyio.run(lambda: _call(app, "/api/v1/test/upload", [broken], upload))
    assert r.status in (400, 422) and calls == [512]  # refused as malformed, not reached


@pytest.mark.parametrize(
    ("chunks", "gap", "then"),
    [
        (pieces(json_body(LIMIT), 25), 0.05, "end"),  # trickled: 40 chunks over 2 s
        ([b'{"x": "'], 0.0, "hang"),  # started, never finished
    ],
    ids=["trickle", "incomplete"],
)
def test_a_body_that_does_not_arrive_in_time_is_refused(
    chunks: list[bytes], gap: float, then: str
) -> None:
    app, calls = make_app()
    started = time.monotonic()
    r = call(app, "/api/v1/test/json", chunks, gap=gap, then=then)
    elapsed = time.monotonic() - started
    refused(r, 408, "request_timeout")
    assert calls == [] and SECONDS <= elapsed < SECONDS + 1.0


def test_a_client_that_leaves_mid_body_reaches_no_route() -> None:
    app, calls = make_app()
    r = call(app, "/api/v1/test/json", pieces(json_body(500), 50)[:3], then="disconnect")
    assert calls == [] and r.taken == 3


def test_waiting_for_a_disconnect_after_the_body_is_not_limited_in_time() -> None:
    """After the body, a receive waits for the client to leave (a streaming response does
    this for as long as it streams); that is not the body arriving slowly."""
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        while (await receive()).get("more_body"):
            pass
        seen.append((await receive())["type"])  # outlives the body's time
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"c", "more_body": False},
    ]

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        await anyio.sleep(SECONDS * 2)
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        pass

    from opsbridge.api.body_limits import BodyLimit

    limited = BodyLimit(inner, max_bytes=LIMIT, max_seconds=SECONDS)
    anyio.run(lambda: limited({"type": "http", "headers": []}, receive, send))
    assert seen == ["http.disconnect"]


def test_over_a_socket_the_refusal_arrives_while_the_client_is_still_sending() -> None:
    app, calls = make_app(max_request_body_seconds=30.0)
    with serve(app) as base:
        port = int(base.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"POST /api/v1/test/json HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
            )
            sent, answer = 0, b""
            for _ in range(200):  # 200 chunks of 100 bytes: twenty times the limit
                try:
                    sock.sendall(b"64\r\n" + b"x" * 100 + b"\r\n")
                except OSError:  # the server may close once it has answered
                    break
                sent += 1
                if select.select([sock], [], [], 0.02)[0]:
                    break
            while b"\r\n\r\n" not in answer:
                more = sock.recv(4096)
                if not more:
                    break
                answer += more
    assert answer.startswith(b"HTTP/1.1 413 "), answer[:200]
    assert sent < 200 and calls == []
