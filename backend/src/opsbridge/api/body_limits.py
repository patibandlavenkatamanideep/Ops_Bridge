"""Request bodies bounded by the bytes actually received, and in time (review N02).

The perimeter refused a declared Content-Length over the limit and every chunked body, and
trusted the rest. A body framed neither way (HTTP/2 has no chunked coding and need not
declare a length) passed both checks; the application read it to its end, and FastAPI parsed
a multipart upload into a temporary file before the upload route compared its size. A body
could also arrive as slowly as its client liked.

`BodyLimit` wraps `receive`: it counts the bytes of every body message and raises 413 in the
reader as soon as the count passes the limit, so the reader stops there (FastAPI re-raises an
HTTPException from body parsing; Starlette closes the multipart files it opened). Until the
body is complete each receive is limited to the time left of the body's budget, and an
expired budget raises 408. Receives after the body (a response waiting for the client to
leave) are not the body's and are passed through. The edge proxy should apply the same limits
(docs/deployment.md); this holds if its configuration drifts.
"""

from __future__ import annotations

import anyio
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimit:
    def __init__(self, app: ASGIApp, *, max_bytes: int, max_seconds: float) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        deadline = anyio.current_time() + self.max_seconds
        received = 0
        complete = False

        async def bounded() -> Message:
            nonlocal received, complete
            if complete:
                return await receive()
            with anyio.CancelScope(deadline=deadline) as wait:
                message = await receive()
            if wait.cancelled_caught:
                raise HTTPException(408, "The request body was not received in time.")
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(413, "The request body is too large.")
                complete = not message.get("more_body", False)
            else:  # http.disconnect: nothing more arrives
                complete = True
            return message

        await self.app(scope, bounded, send)
