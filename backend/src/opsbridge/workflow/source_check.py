"""Before a new external write: does the purchasing system still hold what was approved?

Local freshness (`services/freshness.py`) compares a case's evidence with what OpsBridge has
read. It cannot see a change the purchasing system holds but OpsBridge has not read yet: a
rejection recorded upstream after the approval stays invisible until something reads it, and
the investigation's 24-hour window does not close that gap. So before every NEW external write
(a follow-up task), the executor asks the purchasing system itself:

- **What it compares:** the source records the approval rests on. These are the proposal's
  source manifest (the document versions the investigation read), the invoice and the order
  by key, and the complete listing, under the order, of every PO-filed record type the pack
  maps.
- **Unchanged:** every record still holds the bytes OpsBridge read, nothing is new, and nothing
  is gone. The write may go ahead (local admission still runs, as before).
- **Changed:** a record changed, is new, or is no longer held. Nothing is sent. The operation
  ends `cancelled_before_dispatch` / `source_changed`, and the proposal is superseded. Nothing
  is ingested here: the investigation reads the change when it is re-run.
- **Unverified:** the source could not be asked, or cannot answer for now. Nothing is sent.
  The operation ends `cancelled_before_dispatch` / `source_unverified`, and the proposal is
  superseded, so the action is back in review. Causes:
  - an export (it shows the system as it was when taken);
  - an unreachable source, or an unusable answer;
  - a listing longer than the connector reads;
  - no source configured for the executor.

Only a connector that reads the system of record at the moment of each call can verify
(`reads_live`); `connectors/purchasing.py` does, `connectors/exports.py` does not. The check runs
outside any database transaction and stops after `budget_seconds`, well inside the dispatch
job's lease. A change committed upstream after the check is too late for this write, as with
local admission.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsbridge.canonical import sha256_bytes
from opsbridge.connectors.purchasing import CollectionBudgetExhausted
from opsbridge.connectors.source_contract import (
    PurchasingSource,
    SourceRecordInvalid,
    SourceUnavailable,
)
from opsbridge.mapping import PackSpec, RecordType
from opsbridge.persistence.models import InvestigationVersion, Proposal, SourceDocument, Tenant
from opsbridge.services.execution_admission import SOURCE_CHANGED, SOURCE_UNVERIFIED

from .investigation import _RECORDS_BY_PO

PREFIX = "purchasing:"
# Record types filed under the order, listed by the investigation (`_collect`).
PO_FILED = cast(tuple[RecordType, ...], tuple(_RECORDS_BY_PO))
CHANGED = SOURCE_CHANGED
UNVERIFIED = SOURCE_UNVERIFIED


@dataclass(frozen=True)
class Expected:
    """What the purchasing system held, as far as the approval rests on it."""

    org: str
    pack_id: str
    po_number: str | None
    # (collection, key) → the raw digest OpsBridge read; None: OpsBridge read no such record.
    held: dict[tuple[str, str], str | None]
    labels: dict[str, str]  # collection → record type, for messages
    listed: tuple[str, ...]  # collections listed under the order


@dataclass(frozen=True)
class SourceCheck:
    status: Literal["unchanged", "changed", "unverified"]
    problems: tuple[str, ...] = ()
    checked: dict[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> str | None:
        return {"changed": CHANGED, "unverified": UNVERIFIED}.get(self.status)

    @property
    def detail(self) -> str:
        if self.status == "changed":
            return (
                "nothing was sent: the purchasing system changed after the investigation ("
                + "; ".join(self.problems)
                + "); re-run the investigation to read it"
            )
        if self.status == "unverified":
            return (
                "nothing was sent: OpsBridge could not confirm the purchasing system still holds "
                "what was approved (" + "; ".join(self.problems) + "); the action is back in review"
            )
        return "the purchasing system still holds what was approved"


def expected_for(
    s: Session, op_proposal_id: uuid.UUID, pack_for: Callable[[str], PackSpec]
) -> Expected | str:
    """What to compare, from the approved proposal; a string says why it cannot be known."""
    pr = s.get(Proposal, op_proposal_id)
    if pr is None:
        return "the proposal is missing"
    iv = s.get(InvestigationVersion, pr.investigation_version_id)
    state = (iv.evidence_state if iv is not None else None) or {}
    if "invoice_number" not in state:
        return "the investigation does not record which invoice and order it read"
    tenant = s.get(Tenant, pr.tenant_id)
    if tenant is None:
        return "the tenant is missing"
    pack = pack_for(tenant.pack_id)
    labels = {
        spec.collection: str(rtype) for rtype, spec in pack.sources.items() if spec.collection
    }
    digests = {
        uuid.UUID(m["document_id"]): m["raw_sha256"]
        for m in pr.source_manifest
        if "document_id" in m and "correction_id" not in m
    }
    rows = s.execute(
        select(SourceDocument.id, SourceDocument.source_key).where(SourceDocument.id.in_(digests))
    )
    keys = {doc_id: source_key for doc_id, source_key in rows}
    held: dict[tuple[str, str], str | None] = {}
    for doc_id, source_key in keys.items():
        if source_key.startswith(PREFIX):
            collection, key = source_key.removeprefix(PREFIX).split(":", 1)
            held[(collection, key)] = digests[doc_id]
    by_key: tuple[tuple[RecordType, str | None], ...] = (
        ("invoice", state.get("invoice_number")),
        ("purchase_order", state.get("po_number")),
    )
    for rtype, number in by_key:
        spec = pack.sources.get(rtype)
        if number and spec is not None and spec.collection:
            held.setdefault((spec.collection, number), None)
    listed = tuple(
        spec.collection
        for rtype in PO_FILED
        if (spec := pack.sources.get(rtype)) is not None and spec.collection
    )
    return Expected(tenant.slug, tenant.pack_id, state.get("po_number"), held, labels, listed)


def verify(source: PurchasingSource, expected: Expected, *, budget_seconds: float) -> SourceCheck:
    """Ask the purchasing system whether it still holds `expected`. Closes `source`."""
    checked: dict[str, Any] = {
        "connector": getattr(source, "connector_version", "unknown"),
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        if not getattr(source, "reads_live", False):
            as_of = getattr(source, "as_of", None)
            taken = f", taken {as_of.astimezone(UTC):%Y-%m-%d %H:%M} UTC" if as_of else ""
            return SourceCheck(
                "unverified",
                (
                    f"the purchasing data comes from an export{taken}, which cannot show what "
                    "the purchasing system holds now",
                ),
                checked,
            )
        if hasattr(source, "deadline"):
            source.deadline = time.monotonic() + budget_seconds
        return _compare(source, expected, checked)
    except CollectionBudgetExhausted:
        return SourceCheck(
            "unverified",
            (f"the purchasing system did not answer within {budget_seconds:g} s",),
            checked,
        )
    except SourceUnavailable as exc:
        return SourceCheck(
            "unverified", (f"the purchasing system could not be reached ({exc})",), checked
        )
    except SourceRecordInvalid as exc:
        return SourceCheck(
            "unverified", (f"the purchasing system's answer could not be used ({exc})",), checked
        )
    finally:
        source.close()


def _compare(source: PurchasingSource, expected: Expected, checked: dict[str, Any]) -> SourceCheck:
    def label(collection: str, key: str) -> str:
        return f"{expected.labels.get(collection, collection).replace('_', ' ')} {key}"

    changes: list[str] = []
    fetch = dict(expected.held)
    listings = 0
    if expected.po_number:
        for collection in expected.listed:
            listing = source.list_for_po(collection, expected.po_number)
            listings += 1
            if not listing.complete:
                return SourceCheck(
                    "unverified",
                    (
                        f"the purchasing system lists more {label(collection, '').strip()}s for "
                        f"{expected.po_number} than OpsBridge reads ({listing.pages} pages)",
                    ),
                    checked,
                )
            known = {k for (c, k), want in expected.held.items() if c == collection and want}
            now = set(listing.keys)
            changes += [f"{label(collection, k)} is new" for k in sorted(now - known)]
            for k in sorted(known - now):
                changes.append(f"{label(collection, k)} is no longer held")
                fetch.pop((collection, k), None)
    fetched = 0
    for (collection, key), want in sorted(fetch.items()):
        got = source.fetch(collection, key)
        fetched += 1
        if got is None:
            if want is not None:
                changes.append(f"{label(collection, key)} is no longer held")
        elif want is None:
            changes.append(f"{label(collection, key)} is new")
        elif sha256_bytes(got.body) != want:
            changes.append(f"{label(collection, key)} changed")
    checked |= {"records": fetched, "listings": listings}
    if changes:
        return SourceCheck("changed", tuple(changes), checked)
    return SourceCheck("unchanged", (), checked)
