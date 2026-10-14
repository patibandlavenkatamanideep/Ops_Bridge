"""Read connector for the purchasing system (the local simulator in development).

Credentials are scoped to one org; the base URL comes from configuration (allowlisted),
never from documents or model output. Responses are size-limited and redirects are not
followed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from opsbridge.simulator.app import org_key

CONNECTOR_VERSION = "purchasing-sim/1"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class SourceUnavailable(Exception):
    """The source system could not be reached or answered with a server error."""


class SourceRecordInvalid(Exception):
    """The source returned something unusable (bad status, oversize, bad key)."""


@dataclass(frozen=True)
class FetchedRecord:
    collection: str
    key: str
    body: bytes
    content_type: str
    etag: str | None


class PurchasingClient:
    def __init__(
        self,
        base_url: str,
        master_secret: str,
        org: str,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.org = org
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {org_key(master_secret, org)}"},
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def list_keys(self, collection: str, po_number: str | None = None) -> list[str]:
        params = {"po_number": po_number} if po_number else None
        r = self._get(f"/orgs/{self.org}/{collection}", params=params)
        try:
            items = r.json()["items"]
            return [str(i["key"]) for i in items]
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceRecordInvalid(f"malformed list response for {collection}") from exc

    def fetch(self, collection: str, key: str) -> FetchedRecord | None:
        if not _KEY_RE.match(key):
            raise SourceRecordInvalid("invalid record key")
        try:
            r = self._get(f"/orgs/{self.org}/{collection}/{key}")
        except _NotFound:
            return None
        ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        return FetchedRecord(collection, key, r.content, ctype, r.headers.get("etag"))

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            r = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{type(exc).__name__} contacting purchasing system") from exc
        if r.status_code == 404:
            raise _NotFound()
        if r.status_code >= 500:
            raise SourceUnavailable(f"purchasing system returned {r.status_code}")
        if r.status_code != 200:
            raise SourceRecordInvalid(f"purchasing system returned {r.status_code}")
        if len(r.content) > MAX_RESPONSE_BYTES:
            raise SourceRecordInvalid("response too large")
        return r


class _NotFound(Exception):
    pass
