"""A purchasing source read from an export: files plus an immutable manifest
(contract `purchasing-source/1`, `source_contract.py`).

For an organization whose purchasing system OpsBridge cannot reach, its records arrive as an
export: a directory holding `manifest.json` and the files it names. The manifest says what the
export is and how far it can be trusted, and every file is checked against it before use:

    {
      "schema": "opsbridge-export/1",
      "source_system": "the system and version that produced it",
      "extracted_at": "2026-10-28T09:00:00+00:00",   # the one moment the snapshot shows
      "complete": true,                               # every record in scope is here
      "records": [{"collection": "invoices", "key": "INV-7001", "po_number": "PO-2001",
                   "file": "invoices/INV-7001.json", "content_type": "application/json",
                   "sha256": "<64 hex>", "modified_at": "2026-10-27T16:00:00+00:00"}],
      "withdrawn": [{"collection": "receipts", "key": "GRN-6001X", "po_number": "PO-2001"}]
    }

How the contract's three rules read for an export:
1. A record is absent only when a complete export does not list it, or lists it as
   withdrawn. In a partial export (`complete: false`) absence is unknown, and asking for an
   unlisted record is `SourceRecordInvalid`, never "not found".
2. Listings are complete only when the manifest says so.
3. Withdrawn records are listed as such.

Refused as `SourceRecordInvalid`: an unknown schema, a malformed or oversized manifest, a file
outside the export or missing, a digest that does not match, a record identity listed twice
(or both current and withdrawn), and a record modified after `extracted_at` (a mixed
snapshot). An export that is not there at all is `SourceUnavailable`. The data is as old as
the export: `as_of` is `extracted_at`, which an investigation records as its read time, so the
age limit applies to the export, not to when it was read.

This proves the contract against hand-written exports only; no customer export has been read.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from opsbridge import safejson

from .purchasing import MAX_RESPONSE_BYTES
from .source_contract import FetchedRecord, Listing, SourceRecordInvalid, SourceUnavailable

EXPORT_SCHEMA = "opsbridge-export/1"
CONNECTOR_VERSION = "purchasing-export/1"
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
MAX_RECORDS = 10_000
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_COLLECTION_RE = re.compile(r"^[a-z_]{1,40}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _Entry:
    collection: str
    key: str
    po_number: str | None
    file: Path
    content_type: str
    sha256: str


def _time(value: Any, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise SourceRecordInvalid(f"export {what} is not a time: {value!r}") from exc
    if t.tzinfo is None:
        raise SourceRecordInvalid(f"export {what} has no time zone")
    return t


class ExportSource:
    connector_version = CONNECTOR_VERSION

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        path = self.root / "manifest.json"
        if not path.is_file():
            raise SourceUnavailable(f"no export at {root}")
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise SourceRecordInvalid("export manifest too large")
        try:
            m = safejson.loads(path.read_bytes())
        except ValueError as exc:
            raise SourceRecordInvalid(f"export manifest unreadable: {exc}") from exc
        if not isinstance(m, dict) or m.get("schema") != EXPORT_SCHEMA:
            raise SourceRecordInvalid(f"export schema is not {EXPORT_SCHEMA}")
        self.as_of = _time(m.get("extracted_at"), "extracted_at")
        if not isinstance(m.get("complete"), bool):
            raise SourceRecordInvalid("export does not say whether it is complete")
        self.complete: bool = m["complete"]
        records, withdrawn = m.get("records"), m.get("withdrawn", [])
        if not isinstance(records, list) or not isinstance(withdrawn, list):
            raise SourceRecordInvalid("export records and withdrawals must be lists")
        if len(records) + len(withdrawn) > MAX_RECORDS:
            raise SourceRecordInvalid(f"export lists more than {MAX_RECORDS} records")
        self._records: dict[tuple[str, str], _Entry] = {}
        for r in records:
            entry = self._entry(r)
            ident = (entry.collection, entry.key)
            if ident in self._records:
                raise SourceRecordInvalid(f"export lists {entry.collection} {entry.key} twice")
            self._records[ident] = entry
        self._withdrawn: dict[tuple[str, str], str | None] = {}
        for w in withdrawn:
            if not isinstance(w, dict):
                raise SourceRecordInvalid("export withdrawal is not an object")
            ident = (self._collection(w.get("collection")), self._key(w.get("key")))
            if ident in self._records or ident in self._withdrawn:
                raise SourceRecordInvalid(
                    f"export lists {ident[0]} {ident[1]} as withdrawn and again"
                )
            self._withdrawn[ident] = self._po(w.get("po_number"))

    @staticmethod
    def _collection(value: Any) -> str:
        if not isinstance(value, str) or not _COLLECTION_RE.match(value):
            raise SourceRecordInvalid(f"export collection {value!r} is not valid")
        return value

    @staticmethod
    def _key(value: Any) -> str:
        if not isinstance(value, str) or not _KEY_RE.match(value):
            raise SourceRecordInvalid(f"export record key {value!r} is not valid")
        return value

    @staticmethod
    def _po(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _KEY_RE.match(value):
            raise SourceRecordInvalid(f"export PO number {value!r} is not valid")
        return value

    def _entry(self, r: Any) -> _Entry:
        if not isinstance(r, dict):
            raise SourceRecordInvalid("export record is not an object")
        name = r.get("file")
        if not isinstance(name, str) or not name or Path(name).is_absolute():
            raise SourceRecordInvalid(f"export file name {name!r} is not a relative path")
        file = (self.root / name).resolve()
        if self.root not in file.parents:
            raise SourceRecordInvalid(f"export file {name!r} is outside the export")
        sha = r.get("sha256")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise SourceRecordInvalid(f"export file {name!r} has no SHA-256")
        if "modified_at" in r and _time(r["modified_at"], "modified_at") > self.as_of:
            # Changed after the moment the export claims to show: two snapshots mixed.
            raise SourceRecordInvalid(
                f"export record {r.get('key')!r} was modified after extracted_at (mixed snapshot)"
            )
        content_type = r.get("content_type")
        if not isinstance(content_type, str) or not content_type:
            raise SourceRecordInvalid(f"export file {name!r} has no content type")
        return _Entry(
            self._collection(r.get("collection")),
            self._key(r.get("key")),
            self._po(r.get("po_number")),
            file,
            content_type,
            sha,
        )

    def fetch(self, collection: str, key: str) -> FetchedRecord | None:
        if not _KEY_RE.match(key):
            raise SourceRecordInvalid("invalid record key")
        ident = (collection, key)
        if ident in self._withdrawn:
            return None
        entry = self._records.get(ident)
        if entry is None:
            if self.complete:
                return None
            raise SourceRecordInvalid(
                f"{collection} {key} is not in this partial export; whether it exists is unknown"
            )
        if not entry.file.is_file():
            raise SourceRecordInvalid(f"export file for {collection} {key} is missing")
        if entry.file.stat().st_size > MAX_RESPONSE_BYTES:
            raise SourceRecordInvalid(f"export file for {collection} {key} is too large")
        body = entry.file.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry.sha256:
            raise SourceRecordInvalid(
                f"export file for {collection} {key} does not match its digest"
            )
        return FetchedRecord(collection, key, body, entry.content_type, entry.sha256)

    def list_for_po(self, collection: str, po_number: str) -> Listing:
        keys = tuple(
            e.key
            for e in self._records.values()
            if (e.collection, e.po_number) == (collection, po_number)
        )
        withdrawn = tuple(
            k for (c, k), po in self._withdrawn.items() if (c, po) == (collection, po_number)
        )
        return Listing(collection, po_number, keys, withdrawn, self.complete, 1)

    def listed(self) -> list[tuple[str, str, str | None]]:
        """Every current record as (collection, key, PO it is listed under), in manifest
        order, without reading any file."""
        return [(e.collection, e.key, e.po_number) for e in self._records.values()]

    def holds(self, collection: str, key: str) -> bool | None:
        """Whether the export holds this record, as `fetch` would answer without reading it:
        None when a partial export does not list it, since its existence is then unknown."""
        ident = (collection, key)
        if ident in self._records:
            return True
        return False if self.complete or ident in self._withdrawn else None

    def close(self) -> None:
        pass
