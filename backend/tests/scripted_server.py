"""A loopback TCP server that answers one connection with scripted bytes at scripted times
(review N07: wall-clock tests of total deadlines). It records whether the client closed the
connection, so a test can see that a refused response was not left open."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

Step = tuple[float, bytes]  # wait this long, then send these bytes


class Script:
    def __init__(self, steps: Sequence[Step], *, hold: float = 10.0) -> None:
        self.steps, self.hold = list(steps), hold
        self.client_closed = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _run(self) -> None:
        self._listener.settimeout(10)
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(0.05)
            for wait, data in [*self.steps, (self.hold, b"")]:
                until = time.monotonic() + wait
                while time.monotonic() < until:  # notice a close while waiting
                    try:
                        if conn.recv(65536) == b"":
                            self.client_closed.set()
                            return
                    except TimeoutError:
                        pass
                    except OSError:
                        self.client_closed.set()
                        return
                try:
                    if data:
                        conn.sendall(data)
                except OSError:
                    self.client_closed.set()
                    return


@contextmanager
def scripted(steps: Sequence[Step], *, hold: float = 10.0) -> Iterator[Script]:
    script = Script(steps, hold=hold)
    script._thread.start()
    try:
        yield script
    finally:
        script._listener.close()


def trickle(data: bytes, every: float) -> list[Step]:
    """`data` one byte at a time, `every` seconds apart."""
    return [(every, data[i : i + 1]) for i in range(len(data))]


OK_HEADERS = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n"
