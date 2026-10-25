"""GitHub Issues sandbox connector (follow-up work items only; not an ERP integration).

Verified against GitHub REST docs (fetched 2026-10-14; API version header 2026-03-10):
- `POST /repos/{owner}/{repo}/issues` → 201; errors 400/403/404/410/422/503.
  Fine-grained token permission: repository "Issues: write".
- `GET /repos/{owner}/{repo}/issues/{issue_number}`.
- `GET /repos/{owner}/{repo}/issues?state=all&since=&sort=created&direction=desc&per_page=`
  (includes pull requests; we filter on our marker).
GitHub offers no idempotency key for issue creation, and listing/search consistency is
not guaranteed, so lookup is declared EVENTUAL: an empty result after an ambiguous create
is inconclusive and the executor marks the operation `outcome_unknown` (no re-create).

Safety rails: only the single configured sandbox repository is reachable; live writes
require explicit authorization in configuration; only synthetic/redacted text is sent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from opsbridge.config import Settings

from .tasks import (
    AmbiguousFailure,
    ConnectorCapabilities,
    ConnectorUnavailable,
    DefiniteFailure,
    LookupUnavailable,
    RemoteTask,
    TaskRequest,
)

API_VERSION = "2026-03-10"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_MARKER_RE = re.compile(r"OpsBridge operation marker: (opsbridge-op:[A-Za-z0-9-]+)")


class ConnectorNotAuthorized(ConnectorUnavailable):
    """Configuration does not authorize this connector (repository or token)."""


class GitHubIssuesConnector:
    name = "github_issues"
    version = f"github-issues/{API_VERSION}"
    capabilities = ConnectorCapabilities(
        idempotency_keys=False,
        lookup="eventual",
        conditional_writes=False,
        definite_failure_statuses=frozenset({400, 401, 403, 404, 410, 422}),
        retryable_definite_statuses=frozenset({429}),
        notes="No idempotency for issue creation; list lookup not guaranteed consistent; 5xx/timeouts ambiguous. "
        "403/429 with rate-limit headers are definite (request not processed) and retryable.",
    )

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        allowlist: frozenset[str],
        writes_authorized: bool,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not _REPO_RE.match(repo):
            raise ConnectorNotAuthorized("invalid repository name")
        if repo.lower() not in {r.lower() for r in allowlist}:
            raise ConnectorNotAuthorized(
                f"repository {repo} is not the configured sandbox repository"
            )
        if not token:
            raise ConnectorNotAuthorized("no GitHub token configured")
        self.repo = repo
        self.writes_authorized = writes_authorized
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            follow_redirects=False,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "opsbridge-sandbox-connector",
            },
        )

    def _task(self, d: dict[str, Any]) -> RemoteTask:
        m = _MARKER_RE.search(d.get("body") or "")
        return RemoteTask(
            remote_ref=str(d["number"]),
            url=d.get("html_url"),
            marker=m.group(1) if m else None,
            title=str(d.get("title", "")),
            raw={"number": d["number"], "html_url": d.get("html_url")},
        )

    @staticmethod
    def _rate_limited(r: httpx.Response) -> bool:
        return r.status_code == 429 or (
            r.status_code == 403
            and (r.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in r.headers)
        )

    def create(self, req: TaskRequest, idempotency_key: str | None, timeout: float) -> RemoteTask:
        if not self.writes_authorized:
            raise DefiniteFailure(
                "live GitHub writes are not authorized in configuration", retryable=False
            )
        body = {"title": req.title[:256], "body": req.body}
        try:
            r = self._client.post(f"/repos/{self.repo}/issues", json=body, timeout=timeout)
        except (
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
        ) as exc:
            raise AmbiguousFailure(f"{type(exc).__name__} creating issue") from exc
        except httpx.ConnectError as exc:
            raise DefiniteFailure(f"connection refused: {exc}", retryable=True) from exc
        if r.status_code == 201:
            return self._task(r.json())
        if self._rate_limited(r):
            raise DefiniteFailure(
                "rate limited (not processed)", retryable=True, status=r.status_code
            )
        if r.status_code in self.capabilities.definite_failure_statuses:
            raise DefiniteFailure(
                f"GitHub rejected the issue ({r.status_code})",
                retryable=False,
                status=r.status_code,
            )
        raise AmbiguousFailure(f"GitHub returned {r.status_code}; the issue may exist")

    def get(self, remote_ref: str, timeout: float) -> RemoteTask | None:
        if not remote_ref.isdigit():
            return None
        try:
            r = self._client.get(f"/repos/{self.repo}/issues/{remote_ref}", timeout=timeout)
        except httpx.HTTPError as exc:
            raise LookupUnavailable(f"read-back failed: {type(exc).__name__}") from exc
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            raise LookupUnavailable(f"read-back returned {r.status_code}")
        return self._task(r.json())

    def find_by_marker(self, marker: str, timeout: float) -> list[RemoteTask]:
        since = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        found: list[RemoteTask] = []
        for page in (1, 2, 3):
            try:
                r = self._client.get(
                    f"/repos/{self.repo}/issues",
                    timeout=timeout,
                    params={
                        "state": "all",
                        "since": since,
                        "sort": "created",
                        "direction": "desc",
                        "per_page": 100,
                        "page": page,
                    },
                )
            except httpx.HTTPError as exc:
                raise LookupUnavailable(f"issue listing failed: {type(exc).__name__}") from exc
            if r.status_code != 200:
                raise LookupUnavailable(f"issue listing returned {r.status_code}")
            items = r.json()
            found += [
                self._task(d)
                for d in items
                if "pull_request" not in d and marker in (d.get("body") or "")
            ]
            if len(items) < 100:
                break
        return found


def github_connector_from_settings(settings: Settings) -> GitHubIssuesConnector:
    """The deployment's sandbox repository with its default token (the live test uses it;
    dispatch builds connectors from the tenant's connection instead)."""
    repo = settings.github_sandbox_repo or ""
    token = settings.github_token.get_secret_value() if settings.github_token else ""
    return GitHubIssuesConnector(
        repo,
        token,
        allowlist=frozenset({repo}) if repo else frozenset(),
        writes_authorized=settings.github_live_writes_authorized,
        base_url=settings.github_api_url,
    )
