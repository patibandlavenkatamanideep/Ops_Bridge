"""What OpsBridge requires of a purchasing-source adapter (contract `purchasing-source/1`).

An investigation reads the invoice and purchase order by key and lists the receipts,
allocations and supplier messages filed against the PO. It concludes from what the source
holds at that moment, so an adapter must make three things explicit that a plain HTTP client
leaves implicit:

1. Absence is a fact only when the source states it. `fetch` returns None only when the
   source says the record does not exist (or was deleted). Unreachable, timed out or server
   errors raise `SourceUnavailable` (the step is retried); anything else unusable (refused
   credentials, a bad status, an oversize or malformed body, a redirect) raises
   `SourceRecordInvalid`. Neither may be reported as "not found".
2. A listing is complete or says it is not. `list_for_po` follows the source's pages to the
   end. A page that fails raises; a partial listing is never returned as complete. A cursor
   that does not advance is `SourceRecordInvalid`. When the adapter's page or item bound is
   reached first, it returns what it read with `complete=False`, and OpsBridge concludes
   nothing from that listing.
3. Withdrawals are visible. Records the source reports as deleted, voided or reversed are
   listed in `withdrawn`. OpsBridge also treats a record it ingested from this source that a
   complete listing no longer includes as withdrawn (deleted outright, or moved to another
   PO). Withdrawn records stop being current; their history is kept.

Keys are the source's stable record identifiers. Credentials are scoped to one organization
and the base URL comes from configuration, never from documents or model output.

Only the purchasing simulator implements this contract today
(`connectors/purchasing.py`); no customer purchasing system has been connected or tested
against it. `tests/contracts/test_purchasing_source_contract.py` is the suite an adapter for
a real system must pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CONTRACT = "purchasing-source/1"


class SourceUnavailable(Exception):
    """The source system could not be reached or answered with a server error."""


class SourceRecordInvalid(Exception):
    """The source returned something unusable (bad status, oversize, bad key or cursor)."""


@dataclass(frozen=True)
class FetchedRecord:
    collection: str
    key: str
    body: bytes
    content_type: str
    etag: str | None


@dataclass(frozen=True)
class Listing:
    collection: str
    po_number: str
    keys: tuple[str, ...]  # records the source holds now, in the source's order
    withdrawn: tuple[str, ...]  # records the source reports as deleted, voided or reversed
    complete: bool  # every page was read
    pages: int


class PurchasingSource(Protocol):
    connector_version: str

    def fetch(self, collection: str, key: str) -> FetchedRecord | None: ...

    def list_for_po(self, collection: str, po_number: str) -> Listing: ...

    def close(self) -> None: ...
