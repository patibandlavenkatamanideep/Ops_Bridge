"""Read connector for the purchasing system (the local simulator in development).

Implements the `purchasing-source/1` contract (`source_contract.py`). Credentials are scoped
to one org; the base URL comes from configuration (allowlisted), never from documents or
model output. Responses are read through `http_transport`: streamed, stopped at the size
limit as they arrive (not after buffering) and at a total deadline; redirects are not
followed.
"""

from __future__ import annotations

import re

import httpx

from opsbridge import safejson
from opsbridge.simulator.app import org_key

from . import http_transport
from .source_contract import FetchedRecord, Listing, SourceRecordInvalid, SourceUnavailable

__all__ = ["FetchedRecord", "PurchasingClient", "SourceRecordInvalid", "SourceUnavailable"]

CONNECTOR_VERSION = "purchasing-sim/2"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 50  # a listing longer than this is reported incomplete, not cut silently
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class PurchasingClient:
    connector_version = CONNECTOR_VERSION

    def __init__(
        self,
        base_url: str,
        master_secret: str,
        org: str,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.org = org
        # A whole read, slow drip included, may take a few per-request timeouts at most.
        self._total_seconds = 3 * timeout
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {org_key(master_secret, org)}"},
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def list_for_po(self, collection: str, po_number: str) -> Listing:
        keys: dict[str, None] = {}  # ordered, without the duplicates a shifting page can repeat
        withdrawn: dict[str, None] = {}
        cursor: str | None = None
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            params = {"po_number": po_number, "limit": str(PAGE_SIZE)}
            if cursor is not None:
                params["cursor"] = cursor
            try:
                r = self._get(f"/orgs/{self.org}/{collection}", params=params)
            except _NotFound as exc:  # a listing exists for every PO, if only empty
                raise SourceRecordInvalid(f"no listing for {collection}") from exc
            try:
                body = safejson.response_json(r)
                items, cursor = body["items"], body.get("next_cursor")
                for item in items:
                    key = str(item["key"])
                    if item.get("deleted") is True:
                        withdrawn[key] = None
                        keys.pop(key, None)
                    else:
                        keys[key] = None
                        withdrawn.pop(key, None)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise SourceRecordInvalid(f"malformed list response for {collection}") from exc
            if cursor is None:
                return Listing(collection, po_number, tuple(keys), tuple(withdrawn), True, page)
            if not isinstance(cursor, str) or not _KEY_RE.match(cursor) or cursor in seen:
                raise SourceRecordInvalid(f"list cursor for {collection} did not advance")
            seen.add(cursor)
        return Listing(collection, po_number, tuple(keys), tuple(withdrawn), False, MAX_PAGES)

    def fetch(self, collection: str, key: str) -> FetchedRecord | None:
        if not _KEY_RE.match(key):
            raise SourceRecordInvalid("invalid record key")
        try:
            r = self._get(f"/orgs/{self.org}/{collection}/{key}")
        except _NotFound:
            return None
        ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        return FetchedRecord(collection, key, r.content, ctype, r.headers.get("etag"))

    def _get(
        self, path: str, params: dict[str, str] | None = None
    ) -> http_transport.BoundedResponse:
        try:
            r = http_transport.request(
                self._client,
                "GET",
                path,
                params=params,
                max_bytes=MAX_RESPONSE_BYTES,
                total_seconds=self._total_seconds,
            )
        except (http_transport.ResponseTooLarge, http_transport.UnsupportedEncoding) as exc:
            raise SourceRecordInvalid(f"response too large or unreadable: {exc}") from exc
        except httpx.HTTPError as exc:  # includes the total deadline
            raise SourceUnavailable(f"{type(exc).__name__} contacting purchasing system") from exc
        if r.status_code in (404, 410):
            raise _NotFound()
        if r.status_code >= 500:
            raise SourceUnavailable(f"purchasing system returned {r.status_code}")
        if r.status_code != 200:
            raise SourceRecordInvalid(f"purchasing system returned {r.status_code}")
        return r


class _NotFound(Exception):
    pass
