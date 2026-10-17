"""Block network egress beyond loopback while an offline evaluation runs.

Covers Python-level sockets (httpx, model SDKs, MCP clients) and name resolution. The
PostgreSQL driver (libpq) opens its own sockets in C and is not covered; it connects only
to the configured local database. This is a guard against accidental live calls during
replay, not a sandbox.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class EgressBlocked(RuntimeError):
    """A non-loopback connection or lookup was attempted during an offline run."""


def _is_loopback(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    if host.lower() in ("localhost", "localhost.", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@contextmanager
def loopback_only() -> Iterator[None]:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def check(address: Any) -> None:
        if isinstance(address, (str, bytes)):  # AF_UNIX path
            return
        host = address[0] if isinstance(address, tuple) and address else address
        if not _is_loopback(host):
            raise EgressBlocked(f"network egress to {host!r} is blocked during offline evaluation")

    def connect(self: socket.socket, address: Any) -> None:
        check(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_loopback(host):
            raise EgressBlocked(f"name lookup for {host!r} is blocked during offline evaluation")
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        check(address)
        return real_create_connection(address, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo
    socket.create_connection = create_connection
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo
        socket.create_connection = real_create_connection
