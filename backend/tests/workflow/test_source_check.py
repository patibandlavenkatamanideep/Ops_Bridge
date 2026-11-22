"""Before a new external write: is the purchasing system still what was approved?

`verify` compares what the approval rests on with what a live source holds now, and holds the
write whenever it cannot tell (workflow/source_check.py). The stack tests show the executor
never calls the outbound connector when this says no
(tests/integration/test_dispatch_source_check.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from opsbridge.canonical import sha256_bytes
from opsbridge.connectors.purchasing import CollectionBudgetExhausted
from opsbridge.connectors.source_contract import (
    FetchedRecord,
    Listing,
    SourceRecordInvalid,
    SourceUnavailable,
)
from opsbridge.workflow.investigation import _RECORDS_BY_PO
from opsbridge.workflow.source_check import PO_FILED, Expected, SourceCheck, verify

INVOICE, ORDER, RECEIPT = b'{"invoice": 1}', b'{"po": 1}', b"receipt_id,qty\nGR-1,20\n"
LABELS = {
    "invoices": "invoice",
    "purchase_orders": "purchase_order",
    "receipts": "goods_receipt",
    "receiving_events": "receiving_event",
}


def expected(**held: Any) -> Expected:
    base: dict[tuple[str, str], str | None] = {
        ("invoices", "INV-1"): sha256_bytes(INVOICE),
        ("purchase_orders", "PO-1"): sha256_bytes(ORDER),
        ("receipts", "GR-1"): sha256_bytes(RECEIPT),
    }
    base |= {tuple(k.split("/")): v for k, v in held.items()}  # type: ignore[misc]
    return Expected("demo", "furniture", "PO-1", base, LABELS, ("receipts", "receiving_events"))


class Source:
    """A live purchasing source holding `records`, listed under PO-1 by collection."""

    connector_version = "fake/1"
    reads_live = True

    def __init__(
        self,
        records: dict[tuple[str, str], bytes] | None = None,
        *,
        complete: bool = True,
        fail: Exception | None = None,
    ) -> None:
        self.records = (
            {
                ("invoices", "INV-1"): INVOICE,
                ("purchase_orders", "PO-1"): ORDER,
                ("receipts", "GR-1"): RECEIPT,
            }
            if records is None
            else records
        )
        self.complete = complete
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []
        self.closed = False

    def fetch(self, collection: str, key: str) -> FetchedRecord | None:
        self.calls.append(("fetch", collection, key))
        if self.fail is not None:
            raise self.fail
        body = self.records.get((collection, key))
        return None if body is None else FetchedRecord(collection, key, body, "text/csv", None)

    def list_for_po(self, collection: str, po_number: str) -> Listing:
        self.calls.append(("list", collection, po_number))
        if self.fail is not None:
            raise self.fail
        keys = tuple(k for (c, k) in self.records if c == collection)
        return Listing(collection, po_number, keys, (), self.complete, 1 if self.complete else 50)

    def close(self) -> None:
        self.closed = True


def check(source: Any, exp: Expected | None = None) -> SourceCheck:
    result = verify(source, exp or expected(), budget_seconds=5)
    assert source.closed
    return result


def test_unchanged_when_every_record_and_listing_is_what_was_read() -> None:
    result = check(Source())
    assert (result.status, result.problems, result.category) == ("unchanged", (), None)
    assert (result.checked["records"], result.checked["listings"]) == (3, 2)
    assert result.checked["connector"] == "fake/1"


def test_a_rejection_recorded_upstream_after_approval_is_a_change() -> None:
    src = Source()
    src.records[("receiving_events", "REJ-1")] = b"event_id,event\nREJ-1,REJECTED\n"
    result = check(src)
    assert (result.status, result.category) == ("changed", "source_changed")
    assert result.problems == ("receiving event REJ-1 is new",)
    assert result.detail == (
        "nothing was sent: the purchasing system changed after the investigation (receiving "
        "event REJ-1 is new); re-run the investigation to read it"
    )


def test_a_receipt_edited_upstream_is_a_change() -> None:
    src = Source()
    src.records[("receipts", "GR-1")] = (
        b"receipt_id,qty,status\nGR-1,18,ACCEPTED\nGR-1,2,REJECTED\n"
    )
    assert check(src).problems == ("goods receipt GR-1 changed",)


def test_a_record_gone_upstream_is_a_change_and_is_not_fetched() -> None:
    src = Source()
    del src.records[("receipts", "GR-1")]
    result = check(src)
    assert result.problems == ("goods receipt GR-1 is no longer held",)
    assert ("fetch", "receipts", "GR-1") not in src.calls


def test_the_invoice_or_order_gone_or_newly_present_is_a_change() -> None:
    src = Source()
    del src.records[("invoices", "INV-1")]
    assert check(src).problems == ("invoice INV-1 is no longer held",)
    # The investigation did not find the order; the source holds it now.
    assert check(Source(), expected(**{"purchase_orders/PO-1": None})).problems == (
        "purchase order PO-1 is new",
    )


def test_every_change_is_listed_not_only_the_first() -> None:
    src = Source()
    src.records[("receipts", "GR-1")] = b"changed"
    src.records[("receiving_events", "REJ-1")] = b"new"
    assert check(src).problems == ("receiving event REJ-1 is new", "goods receipt GR-1 changed")


def test_without_an_order_only_the_invoice_is_asked_for() -> None:
    exp = Expected(
        "demo", "furniture", None, {("invoices", "INV-1"): sha256_bytes(INVOICE)}, LABELS, ()
    )
    src = Source()
    assert check(src, exp).status == "unchanged"
    assert src.calls == [("fetch", "invoices", "INV-1")]


@pytest.mark.parametrize(
    ("failure", "phrase"),
    [
        (SourceUnavailable("ConnectError contacting purchasing system"), "could not be reached"),
        (SourceRecordInvalid("purchasing system returned 403"), "answer could not be used"),
        (CollectionBudgetExhausted("budget"), "did not answer within 5 s"),
    ],
)
def test_a_source_that_cannot_answer_holds_the_write(failure: Exception, phrase: str) -> None:
    result = check(Source(fail=failure))
    assert (result.status, result.category) == ("unverified", "source_unverified")
    [problem] = result.problems
    assert phrase in problem
    assert result.detail.startswith("nothing was sent: OpsBridge could not confirm")
    assert result.detail.endswith("the action is back in review")


def test_a_listing_longer_than_the_connector_reads_holds_the_write() -> None:
    result = check(Source(complete=False))
    assert result.status == "unverified"
    assert result.problems == (
        "the purchasing system lists more goods receipts for PO-1 than OpsBridge reads (50 pages)",
    )


def test_an_export_cannot_confirm_the_system_now_and_is_not_asked() -> None:
    class Export(Source):
        reads_live = False
        as_of = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)

    src = Export()
    result = check(src)
    assert result.status == "unverified"
    assert result.problems == (
        "the purchasing data comes from an export, taken 2026-11-17 08:00 UTC, which cannot "
        "show what the purchasing system holds now",
    )
    assert src.calls == []


def test_a_source_that_does_not_declare_itself_live_is_not_trusted() -> None:
    # The same source, with no `reads_live` at all.
    members = {k: v for k, v in vars(Source).items() if k not in ("reads_live", "__dict__")}
    src = type("Undeclared", (), members)()
    assert not hasattr(src, "reads_live")
    assert check(src).status == "unverified"
    assert src.calls == []


def test_it_lists_every_record_type_the_investigation_lists_under_the_order() -> None:
    assert tuple(_RECORDS_BY_PO) == PO_FILED
    assert set(PO_FILED) == {
        "goods_receipt",
        "allocation_history",
        "supplier_message",
        "receiving_event",
    }
