"""Block network egress beyond loopback while an offline evaluation runs.

Covers Python-level sockets (httpx, model SDKs, MCP clients) and name resolution. The
PostgreSQL driver (libpq) opens its own sockets in C and is not covered; it connects only
to the configured local database. This is a guard against accidental live calls during
replay, not a sandbox: native code can open sockets it does not see.

Proxies (review N08). A loopback proxy is loopback too: with HTTPS_PROXY naming one, an
httpx client connected to it, which was allowed, and asked it to CONNECT to the provider,
which the proxy would resolve and reach itself. While the guard holds, proxy settings
(HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, the SOCKS and other variants, in either case) are
removed from the environment, so a client made inside it uses none, and every address they
named is refused, so a client that read one earlier cannot reach it. An offline run that
registers the loopback ports it needs (`loopback_only(ports=set())`, then `guard.allow`)
reaches only those.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

PROXY_VARIABLES = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "WS_PROXY", "WSS_PROXY",
     "SOCKS_PROXY", "SOCKS4_PROXY", "SOCKS5_PROXY"}
)  # fmt: skip
_DEFAULT_PORTS = {"http": 80, "https": 443, "socks4": 1080, "socks5": 1080, "socks5h": 1080}


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


def _proxy_port(value: str) -> int | None:
    """The port a proxy setting names (its scheme's default when it names none)."""
    url = urlsplit(value if "://" in value else f"http://{value}")
    try:
        return url.port or _DEFAULT_PORTS.get(url.scheme.lower())
    except ValueError:
        return None


class Guard:
    def __init__(self, ports: Collection[int] | None, proxy_ports: set[int]) -> None:
        self.ports = set(ports) if ports is not None else None
        self.proxy_ports = proxy_ports

    def allow(self, port: int) -> None:
        """Let this offline run reach a local service on `port` (loopback only)."""
        if self.ports is not None:
            self.ports.add(port)

    def check(self, address: Any) -> None:
        if isinstance(address, (str, bytes)):  # AF_UNIX path
            return
        host = address[0] if isinstance(address, tuple) and address else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        if not _is_loopback(host):
            raise EgressBlocked(f"network egress to {host!r} is blocked during offline evaluation")
        if port in self.proxy_ports:
            raise EgressBlocked(
                f"port {port} on {host!r} is a proxy named in the environment; it is blocked "
                "during offline evaluation"
            )
        if self.ports is not None and port not in self.ports:
            raise EgressBlocked(f"loopback port {port} is not registered for this offline run")


@contextmanager
def loopback_only(*, ports: Collection[int] | None = None) -> Iterator[Guard]:
    named = {k: v for k, v in os.environ.items() if k.upper() in PROXY_VARIABLES}
    guard = Guard(ports, {p for v in named.values() if (p := _proxy_port(v)) is not None})
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def connect(self: socket.socket, address: Any) -> None:
        guard.check(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        guard.check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_loopback(host):
            raise EgressBlocked(f"name lookup for {host!r} is blocked during offline evaluation")
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        guard.check(address)
        return real_create_connection(address, *args, **kwargs)

    for name in named:
        del os.environ[name]
    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo
    socket.create_connection = create_connection
    try:
        yield guard
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo
        socket.create_connection = real_create_connection
        os.environ.update(named)
