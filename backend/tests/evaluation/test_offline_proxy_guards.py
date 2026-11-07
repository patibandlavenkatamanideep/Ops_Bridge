"""Offline evaluation reaches no network through a proxy, nor a live model (review N08).

The offline guard refuses name lookups and connections beyond loopback. A loopback proxy
was loopback too: with HTTPS_PROXY pointing at one, an httpx client connected to it, which
the guard allowed, and asked it to CONNECT to the provider, which the proxy would resolve
and reach itself. Now, while the guard holds, proxy settings are removed from the
environment, an address they named cannot be connected to even by a client made before,
and an offline run may reach only the loopback ports registered for it. A live model mode
is refused before an offline run starts, instead of being routed away silently.

The listener here records what it is sent and forwards nothing; no request leaves this
host. This is protection against accidental egress from Python code, not a sandbox: native
code (libpq included) opens its own sockets.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from opsbridge import cli
from opsbridge.agents.adapters import AnthropicAdapter
from opsbridge.agents.contracts import ModelError, ModelRequest
from opsbridge.evaluation import bench
from opsbridge.evaluation.egress import EgressBlocked, loopback_only

PROVIDER = "https://api.anthropic.com/v1/messages"


class Listener:
    """A loopback proxy that never forwards: it records each connection's first bytes."""

    def __init__(self) -> None:
        self.seen: list[bytes] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = int(self._server.getsockname()[1])
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _run(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1)
                try:
                    self.seen.append(conn.recv(4096))
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass


@contextmanager
def listening() -> Iterator[Listener]:
    listener = Listener()
    listener._thread.start()
    try:
        yield listener
    finally:
        listener._server.close()


@pytest.mark.parametrize(
    "variable", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
)
def test_a_client_made_offline_uses_no_proxy_from_the_environment(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with listening() as proxy:
        monkeypatch.setenv(variable, proxy.url)
        with loopback_only():
            assert variable not in os.environ  # removed while the guard holds
            with pytest.raises(EgressBlocked):
                httpx.get(PROVIDER, timeout=2)
        assert os.environ[variable] == proxy.url  # and restored after
    assert proxy.seen == []


def test_a_socks_setting_reaches_nothing_either(monkeypatch: pytest.MonkeyPatch) -> None:
    with listening() as proxy:
        monkeypatch.setenv("ALL_PROXY", f"socks5://127.0.0.1:{proxy.port}")
        with loopback_only(), pytest.raises(EgressBlocked):
            httpx.get(PROVIDER, timeout=2)
    assert proxy.seen == []


def test_a_client_made_before_the_guard_cannot_reach_its_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with listening() as proxy:
        monkeypatch.setenv("HTTPS_PROXY", proxy.url)
        client = httpx.Client(timeout=2)  # it read the proxy setting already
        with client, loopback_only(), pytest.raises(EgressBlocked, match="proxy"):
            client.get(PROVIDER)
    assert proxy.seen == []


def test_the_model_adapter_does_not_tunnel_through_a_loopback_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = ModelRequest(
        template_id="t", template_digest="sha256:t", system="s", user="u",
        output_schema={"type": "object"}, max_output_tokens=10, timeout_seconds=2.0, params={},
    )  # fmt: skip
    with listening() as proxy:
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(variable, proxy.url)
        with loopback_only(), pytest.raises((ModelError, EgressBlocked)):
            AnthropicAdapter("claude-test", "test-key").generate(request)
    assert proxy.seen == []


def test_an_offline_run_reaches_only_the_local_endpoints_registered_for_it() -> None:
    with (
        socket.create_server(("127.0.0.1", 0)) as wanted,
        socket.create_server(("127.0.0.1", 0)) as other,
    ):
        with loopback_only(ports=set()) as guard:
            guard.allow(wanted.getsockname()[1])
            socket.create_connection(wanted.getsockname()[:2], timeout=1).close()
            with pytest.raises(EgressBlocked, match="not registered"):
                socket.create_connection(other.getsockname()[:2], timeout=1)
        socket.create_connection(other.getsockname()[:2], timeout=1).close()  # guard removed


def test_a_live_model_mode_is_refused_before_an_offline_run_starts(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(os.getcwd())  # the command moves to the repository root
    with pytest.raises(bench.LiveModeRefused, match="anthropic"):
        bench.offline_modes(["rules_only", "anthropic"])
    assert bench.offline_modes(["rules_only", "mock"]) == ["rules_only", "mock"]
    code = cli.main(
        ["eval-bench", "--split", "security", "--replay", "--modes", "rules_only,ollama"]
    )
    assert code == 2 and "ollama" in capsys.readouterr().out
