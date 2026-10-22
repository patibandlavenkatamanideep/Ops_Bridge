"""Follow-up task connectors and their declared execution capabilities.

Each connector states, explicitly:
- whether the provider honours idempotency keys;
- whether an operation can be reconciled by lookup, and how consistent that lookup is
  ("strong": an empty result proves absence; "eventual": an empty result is inconclusive;
  "none": no lookup);
- whether conditional writes are supported;
- which failures are definite (nothing happened) versus ambiguous (it may have happened).
The executor uses these declarations to decide between retry, reconcile and
outcome_unknown. It never retries an ambiguous write blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from opsbridge.simulator.app import org_key

Consistency = Literal["strong", "eventual", "none"]


@dataclass(frozen=True)
class ConnectorCapabilities:
    idempotency_keys: bool
    lookup: Consistency
    conditional_writes: bool
    definite_failure_statuses: frozenset[int]
    retryable_definite_statuses: frozenset[int]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_keys": self.idempotency_keys,
            "lookup": self.lookup,
            "conditional_writes": self.conditional_writes,
            "definite_failure_statuses": sorted(self.definite_failure_statuses),
            "retryable_definite_statuses": sorted(self.retryable_definite_statuses),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TaskRequest:
    title: str
    body: str
    assignee_group: str | None
    marker: str  # opaque marker embedded in the remote task for reconciliation


@dataclass(frozen=True)
class RemoteTask:
    remote_ref: str
    url: str | None
    marker: str | None
    title: str
    raw: dict[str, Any]


class DefiniteFailure(Exception):
    """The provider did not perform the write."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class AmbiguousFailure(Exception):
    """The write may or may not have happened (timeout, dropped response, 5xx)."""


class LookupUnavailable(Exception):
    """Reconciliation lookup is not supported or failed; absence is NOT established."""


class TaskConnector(Protocol):
    name: str
    version: str

    @property
    def capabilities(self) -> ConnectorCapabilities: ...

    def create(
        self, req: TaskRequest, idempotency_key: str | None, timeout: float
    ) -> RemoteTask: ...
    def get(self, remote_ref: str, timeout: float) -> RemoteTask | None: ...
    def find_by_marker(self, marker: str, timeout: float) -> list[RemoteTask]: ...


class SimTaskConnector:
    """HTTP connector for the simulator's task tracker."""

    name = "sim_tasks"
    version = "sim-tasks/1"

    def __init__(
        self,
        base_url: str,
        master_secret: str,
        org: str,
        *,
        idempotency_keys: bool | None = None,
        lookup: Consistency | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.org = org
        self._declared = (idempotency_keys, lookup)
        self._capabilities: ConnectorCapabilities | None = None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Bearer {org_key(master_secret, org)}"},
        )

    @property
    def capabilities(self) -> ConnectorCapabilities:
        """Explicit settings win; otherwise discovered from the tracker, once per instance.

        Discovery failure assumes the weakest contract (no idempotency keys, no lookup), so an
        ambiguous result is sent to human review instead of being retried into a duplicate.
        """
        if self._capabilities is None:
            idem, lookup = self._declared
            if idem is None or lookup is None:
                found_idem, found_lookup = self._discover()
                idem = found_idem if idem is None else idem
                lookup = found_lookup if lookup is None else lookup
            self._capabilities = ConnectorCapabilities(
                idempotency_keys=idem,
                lookup=lookup,
                conditional_writes=False,
                definite_failure_statuses=frozenset({400, 401, 403, 404, 409, 422}),
                retryable_definite_statuses=frozenset({503}),
                notes="503 is definite only with X-Sim-Not-Committed: true; other 5xx and timeouts are "
                "ambiguous. Capabilities discovered from the tracker unless configured.",
            )
        return self._capabilities

    def discover(self) -> tuple[bool, Consistency] | None:
        """The tracker's advertised (idempotency keys, lookup), or None if it could not say."""
        try:
            r = self._client.get(f"/orgs/{self.org}/capabilities", timeout=3.0)
            if r.status_code == 200:
                d = r.json()
                lookup = d.get("lookup")
                if isinstance(d.get("idempotency_keys"), bool) and lookup in (
                    "strong",
                    "eventual",
                    "none",
                ):
                    return d["idempotency_keys"], lookup
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def _discover(self) -> tuple[bool, Consistency]:
        return self.discover() or (False, "none")

    def close(self) -> None:
        self._client.close()

    def _task(self, d: dict[str, Any]) -> RemoteTask:
        return RemoteTask(
            remote_ref=str(d["id"]),
            url=None,
            marker=d.get("marker"),
            title=str(d.get("title", "")),
            raw=d,
        )

    def create(self, req: TaskRequest, idempotency_key: str | None, timeout: float) -> RemoteTask:
        headers = (
            {"Idempotency-Key": idempotency_key}
            if (idempotency_key and self.capabilities.idempotency_keys)
            else {}
        )
        body = {
            "title": req.title,
            "body": req.body,
            "assignee_group": req.assignee_group,
            "marker": req.marker,
        }
        try:
            r = self._client.post(
                f"/orgs/{self.org}/tasks", json=body, headers=headers, timeout=timeout
            )
        except (
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
        ) as exc:
            # The request may have reached the provider and been committed.
            raise AmbiguousFailure(f"{type(exc).__name__} during create") from exc
        except httpx.ConnectError as exc:  # never reached the provider
            raise DefiniteFailure(f"connection refused: {exc}", retryable=True) from exc
        if r.status_code in (200, 201):
            return self._task(r.json())
        if r.status_code == 503 and r.headers.get("x-sim-not-committed") == "true":
            raise DefiniteFailure(
                "provider unavailable (not committed)", retryable=True, status=503
            )
        if r.status_code in self.capabilities.definite_failure_statuses:
            raise DefiniteFailure(
                f"provider rejected the task ({r.status_code})",
                retryable=False,
                status=r.status_code,
            )
        raise AmbiguousFailure(f"provider returned {r.status_code}; the task may exist")

    def get(self, remote_ref: str, timeout: float) -> RemoteTask | None:
        try:
            r = self._client.get(f"/orgs/{self.org}/tasks/{remote_ref}", timeout=timeout)
        except httpx.HTTPError as exc:
            raise LookupUnavailable(f"read-back failed: {type(exc).__name__}") from exc
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise LookupUnavailable(f"read-back returned {r.status_code}")
        return self._task(r.json())

    def find_by_marker(self, marker: str, timeout: float) -> list[RemoteTask]:
        if self.capabilities.lookup == "none":
            raise LookupUnavailable("connector declares no lookup capability")
        try:
            r = self._client.get(
                f"/orgs/{self.org}/tasks", params={"marker": marker}, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise LookupUnavailable(f"lookup failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise LookupUnavailable(f"lookup returned {r.status_code}")
        return [self._task(d) for d in r.json().get("items", [])]
