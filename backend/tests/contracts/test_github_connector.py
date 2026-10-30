"""GitHub Issues connector against an independent fake of the documented API (no network).

The fake's `timeout_after` reply is held until the test is done, not delayed by a timer: a
timer in the server and the client's deadline ran on different clocks (on macOS uvloop's
counts time the host slept, Python's monotonic clock does not), so a host that slept during
the delay received the "late" reply within the deadline and the create returned the issue.
That was the intermittent failure of the timeout test; the connector was right both times.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from opsbridge.connectors.github import ConnectorNotAuthorized, GitHubIssuesConnector
from opsbridge.connectors.tasks import AmbiguousFailure, DefiniteFailure, TaskRequest

from ..integration.servers import serve
from .fake_github import FakeGitHub
from .jumping_clock import serve_jumping

REPO = "opsbridge-sandbox/followups"


@pytest.fixture
def gh() -> Iterator[tuple[FakeGitHub, str]]:
    fake = FakeGitHub(repo=REPO)
    with serve(fake.app(), health_path="/docs") as base:
        try:
            yield fake, base
        finally:
            fake.release_held_replies.set()  # before the server stops: it waits for them


def conn(
    base: str, token: str = "test-token", writes: bool = True, repo: str = REPO
) -> GitHubIssuesConnector:
    return GitHubIssuesConnector(
        repo, token, allowlist=frozenset({REPO}), writes_authorized=writes, base_url=base
    )


REQ = TaskRequest(
    "Confirm delivered quantity for PO-1001",
    "Synthetic body\n\nOpsBridge operation marker: opsbridge-op:abc123",
    "receiving",
    "opsbridge-op:abc123",
)


def test_create_sends_documented_headers_and_parses_issue(gh) -> None:  # type: ignore[no-untyped-def]
    fake, base = gh
    task = conn(base).create(REQ, None, 2.0)
    assert (task.remote_ref, task.marker) == ("1", "opsbridge-op:abc123")
    assert task.url and task.url.endswith("/issues/1")
    sent = next(r for r in fake.requests if r["method"] == "POST")["headers"]
    assert (
        sent["accept"] == "application/vnd.github+json"
        and sent["x-github-api-version"] == "2026-03-10"
    )
    assert "idempotency-key" not in sent  # GitHub has none; we never pretend otherwise
    assert conn(base).get("1", 2.0).marker == "opsbridge-op:abc123"  # type: ignore[union-attr]
    assert conn(base).get("404", 2.0) is None


@pytest.mark.parametrize(
    ("fault", "retryable"), [("422", False), ("410", False), ("rate_limited", True)]
)
def test_definite_failures(gh, fault: str, retryable: bool) -> None:  # type: ignore[no-untyped-def]
    fake, base = gh
    fake.faults.append(fault)
    with pytest.raises(DefiniteFailure) as exc:
        conn(base).create(REQ, None, 2.0)
    assert exc.value.retryable is retryable
    assert fake.issues == []


@pytest.mark.parametrize("fault", ["500_after", "503_before", "timeout_after"])
def test_ambiguous_failures_never_claim_absence(gh, fault: str) -> None:  # type: ignore[no-untyped-def]
    fake, base = gh
    fake.faults.append(fault)
    with pytest.raises(AmbiguousFailure) as exc:
        conn(base).create(REQ, None, 1.0)
    # Ambiguous whether or not the issue exists: it does after 500_after and timeout_after.
    assert len(fake.issues) == (0 if fault == "503_before" else 1)
    if fault == "timeout_after":
        assert isinstance(exc.value.__cause__, httpx.TimeoutException)


def test_a_late_reply_stays_late_when_the_server_clock_jumps() -> None:
    fake = FakeGitHub(repo=REPO)
    with serve_jumping(fake.app()) as (base, loop):
        try:
            fake.faults.append("timeout_after")
            loop.jump_after(0.1, 5.0)  # the host sleeps: 5 s pass for the server, none here
            with pytest.raises(AmbiguousFailure) as exc:
                conn(base).create(REQ, None, 1.0)
            assert isinstance(exc.value.__cause__, httpx.TimeoutException)
            assert len(fake.issues) == 1
        finally:
            fake.release_held_replies.set()


def test_bad_credentials_are_definite(gh) -> None:  # type: ignore[no-untyped-def]
    _fake, base = gh
    with pytest.raises(DefiniteFailure):
        conn(base, token="wrong").create(REQ, None, 2.0)


def test_marker_lookup_filters_pull_requests_and_reports_eventual_consistency(gh) -> None:  # type: ignore[no-untyped-def]
    fake, base = gh
    c = conn(base)
    c.create(REQ, None, 2.0)
    assert [t.remote_ref for t in c.find_by_marker("opsbridge-op:abc123", 2.0)] == ["1"]
    assert c.find_by_marker("opsbridge-op:other", 2.0) == []
    fake.hide_recent_seconds = 60  # a lagging listing: the issue exists but is not listed yet
    assert c.find_by_marker("opsbridge-op:abc123", 2.0) == []
    assert c.capabilities.lookup == "eventual" and c.capabilities.idempotency_keys is False


def test_only_the_configured_sandbox_repository_and_authorized_writes(gh) -> None:  # type: ignore[no-untyped-def]
    _fake, base = gh
    with pytest.raises(ConnectorNotAuthorized):
        conn(base, repo="someone-else/production")
    with pytest.raises(ConnectorNotAuthorized):
        GitHubIssuesConnector(
            REPO, "", allowlist=frozenset({REPO}), writes_authorized=True, base_url=base
        )
    with pytest.raises(DefiniteFailure, match="not authorized"):
        conn(base, writes=False).create(REQ, None, 2.0)
