"""A real HTTP server whose event-loop clock can jump forward while the client's does not.

uvicorn runs uvloop, whose clock on macOS (CLOCK_MONOTONIC) counts the time the host slept;
Python's `time.monotonic()`, which client deadlines use, does not. When the host sleeps, a
server timer meant to answer after a client's deadline can therefore answer before it. This
server reproduces that on demand: `jump(seconds)` moves only the server's clock.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import uvicorn

from opsbridge.evaluation.local_server import free_port


class JumpingLoop(asyncio.SelectorEventLoop):
    offset = 0.0

    def time(self) -> float:
        return super().time() + self.offset

    def jump_after(self, delay: float, seconds: float) -> None:
        """In `delay` seconds, `seconds` pass for this loop's clock and for nothing else."""
        self.call_soon_threadsafe(
            self.call_later, delay, lambda: setattr(self, "offset", self.offset + seconds)
        )


@contextmanager
def serve_jumping(app: Any) -> Iterator[tuple[str, JumpingLoop]]:
    loop = JumpingLoop()
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, loop="none", log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}", loop
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        loop.close()
