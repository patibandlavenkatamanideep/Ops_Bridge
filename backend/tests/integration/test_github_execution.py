"""Executor + GitHub connector against the fake GitHub API (no real GitHub calls)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from opsbridge.connectors.github import GitHubIssuesConnector

from ..contracts.fake_github import FakeGitHub
from .servers import serve
from .test_approvals_execution import approve, drain, operations, prepared

pytestmark = pytest.mark.integration
REPO = "opsbridge-sandbox/followups"


@pytest.fixture
def github(stack) -> Iterator[tuple[FakeGitHub, Any]]:  # type: ignore[no-untyped-def]
    fake = FakeGitHub(repo=REPO)
    original = stack["exec"]["exec"]
    with serve(fake.app(), health_path="/docs") as base:

        def factory(tid: Any, name: str, op: dict[str, Any]) -> GitHubIssuesConnector:
            return GitHubIssuesConnector(
                REPO,
                "test-token",
                allowlist=frozenset({REPO}),
                writes_authorized=True,
                base_url=base,
            )

        stack["exec"]["exec"] = replace(original, connector_for=factory, timeout_seconds=1.0)
        try:
            yield fake, stack
        finally:
            stack["exec"]["exec"] = original


def test_issue_created_and_read_back(github) -> None:  # type: ignore[no-untyped-def]
    fake, st = github
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["remote_ref"] == "1"
    assert len(fake.issues) == 1 and "opsbridge-op:" in fake.issues[0]["body"]


def test_timeout_after_commit_with_lagging_listing_is_outcome_unknown_not_a_duplicate(
    github,
) -> None:  # type: ignore[no-untyped-def]
    fake, st = github
    fake.faults.append("timeout_after")
    fake.hide_recent_seconds = 600  # listing has not caught up with the write
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "outcome_unknown" and op["review_required"] is True
    assert op["failure_category"] == "unreconcilable"
    assert len(fake.issues) == 1  # created once, never blindly re-created


def test_timeout_after_commit_reconciled_when_listing_shows_the_issue(github) -> None:  # type: ignore[no-untyped-def]
    fake, st = github
    fake.faults.append("timeout_after")
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "confirmed" and op["confirmation"]["method"] == "reconciled_by_marker"
    assert len(fake.issues) == 1
