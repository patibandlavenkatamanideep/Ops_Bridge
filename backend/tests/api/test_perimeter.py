"""Perimeter helpers: the address a request comes from, and a bounded fallback limiter
(review finding F09)."""

from __future__ import annotations

from ipaddress import ip_network

from opsbridge.api.middleware import FixedWindowLimiter, client_address

PROXIES = [ip_network("10.0.0.0/8")]


def test_forwarded_addresses_are_believed_only_from_configured_proxies() -> None:
    # Directly connected client: its address, whatever header it sends.
    assert client_address("203.0.113.9", "198.51.100.1", PROXIES) == "203.0.113.9"
    # Through the load balancer: the nearest address it did not add itself.
    assert client_address("10.0.0.5", "198.51.100.1", PROXIES) == "198.51.100.1"
    # A client-supplied first hop cannot choose its bucket: the proxy appended the real one.
    assert client_address("10.0.0.5", "6.6.6.6, 198.51.100.1, 10.0.0.7", PROXIES) == "198.51.100.1"
    # Without configured proxies the header is ignored.
    assert client_address("10.0.0.5", "198.51.100.1", []) == "10.0.0.5"
    assert client_address("10.0.0.5", "not-an-address", PROXIES) == "10.0.0.5"


def test_the_fallback_limiter_never_holds_more_than_its_bound() -> None:
    limiter = FixedWindowLimiter(limit=1, window_seconds=60, max_keys=2)
    allowed = [limiter.hit(f"ip:192.0.2.{i}")[0] for i in range(6)]
    assert len(limiter._counts) <= 3  # two keys plus one shared overflow window
    assert allowed == [True, True, True, False, False, False]  # new keys share the overflow
