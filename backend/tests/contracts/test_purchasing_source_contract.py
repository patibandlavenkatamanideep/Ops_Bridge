"""The `purchasing-source/1` contract (connectors/source_contract.py), checked against the
connector and the simulator, and against sources that misbehave.

An adapter for a real purchasing system must pass the same checks against its sandbox:
`source` becomes the adapter and `Arrange` the sandbox's way of creating, deleting and paging
records. No customer system has been run through this suite.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from opsbridge.connectors import purchasing
from opsbridge.connectors.purchasing import PurchasingClient
from opsbridge.connectors.source_contract import (
    PurchasingSource,
    SourceRecordInvalid,
    SourceUnavailable,
)
from opsbridge.simulator.app import create_simulator
from opsbridge.simulator.store import SimStore

MASTER = "contract-test-master-secret"
ORG = "contract-org"
PO = "PO-1"


def forward(client: TestClient) -> httpx.MockTransport:
    """Send the connector's requests to the in-process simulator."""

    def handle(request: httpx.Request) -> httpx.Response:
        r = client.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    return httpx.MockTransport(handle)


@pytest.fixture
def sim(tmp_path: Path) -> Iterator[tuple[TestClient, SimStore]]:
    app = create_simulator(tmp_path / "sim.sqlite3", MASTER, fault_controls=True, production=False)
    with TestClient(app) as client:
        yield client, app.state.store


def connect(sim: tuple[TestClient, SimStore], secret: str = MASTER) -> PurchasingSource:
    return PurchasingClient("http://sim", secret, ORG, transport=forward(sim[0]))


def receipts(store: SimStore, n: int, po: str = PO, prefix: str = "GR") -> list[str]:
    keys = [f"{prefix}-{i:03d}" for i in range(n)]
    for key in keys:
        store.put_record(
            ORG, "receipts", key, json.dumps({"id": key}).encode(), "application/json", po
        )
    return keys


# --- against the simulator ------------------------------------------------------------


def test_a_listing_follows_every_page(sim) -> None:  # type: ignore[no-untyped-def]
    keys = receipts(sim[1], 7)
    receipts(sim[1], 3, po="PO-OTHER", prefix="OTHER")
    sim[1].set_page_limit(ORG, 2)  # the server serves fewer than the connector asks for
    listing = connect(sim).list_for_po("receipts", PO)
    assert (listing.keys, listing.withdrawn, listing.complete) == (tuple(keys), (), True)
    assert listing.pages == 4


def test_an_empty_listing_is_complete(sim) -> None:  # type: ignore[no-untyped-def]
    listing = connect(sim).list_for_po("receipts", PO)
    assert (listing.keys, listing.complete, listing.pages) == ((), True, 1)


def test_a_deleted_record_is_listed_as_withdrawn_and_not_served(sim) -> None:  # type: ignore[no-untyped-def]
    keys = receipts(sim[1], 3)
    assert sim[1].delete_record(ORG, "receipts", keys[1])
    source = connect(sim)
    listing = source.list_for_po("receipts", PO)
    assert (listing.keys, listing.withdrawn) == ((keys[0], keys[2]), (keys[1],))
    assert source.fetch("receipts", keys[1]) is None
    sim[1].put_record(ORG, "receipts", keys[1], b"{}", "application/json", PO)  # restored
    assert source.list_for_po("receipts", PO).keys == tuple(keys)


def test_a_record_moved_to_another_po_leaves_this_listing(sim) -> None:  # type: ignore[no-untyped-def]
    keys = receipts(sim[1], 2)
    sim[1].put_record(ORG, "receipts", keys[0], b"{}", "application/json", "PO-OTHER")
    assert connect(sim).list_for_po("receipts", PO).keys == (keys[1],)


def test_only_the_source_saying_so_makes_a_record_absent(sim) -> None:  # type: ignore[no-untyped-def]
    receipts(sim[1], 1)
    assert connect(sim).fetch("receipts", "GR-404") is None
    wrong = connect(sim, secret="another-secret")  # refused credentials are not "not found"
    with pytest.raises(SourceRecordInvalid):
        wrong.fetch("receipts", "GR-000")
    with pytest.raises(SourceRecordInvalid):
        wrong.list_for_po("receipts", PO)


@pytest.mark.parametrize("route", ["records.list", "records.get"])
def test_a_server_error_is_unavailable(sim, route: str) -> None:  # type: ignore[no-untyped-def]
    receipts(sim[1], 1)
    sim[0].post("/_control/faults", json={"org": ORG, "route": route, "mode": "fail_500"})
    source = connect(sim)
    with pytest.raises(SourceUnavailable):
        if route == "records.list":
            source.list_for_po("receipts", PO)
        else:
            source.fetch("receipts", "GR-000")


def test_a_listing_longer_than_the_bound_says_it_is_incomplete(
    sim, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    keys = receipts(sim[1], 7)
    sim[1].set_page_limit(ORG, 2)
    monkeypatch.setattr(purchasing, "MAX_PAGES", 2)
    listing = connect(sim).list_for_po("receipts", PO)
    assert (listing.keys, listing.complete, listing.pages) == (tuple(keys[:4]), False, 2)


# --- against sources that misbehave ----------------------------------------------------


def stub(handle: Callable[[httpx.Request], httpx.Response]) -> PurchasingSource:
    return PurchasingClient("http://src", MASTER, ORG, transport=httpx.MockTransport(handle))


def pages(*bodies: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the bodies in turn; past the last one, answer `status`."""
    served = iter(bodies)

    def handle(request: httpx.Request) -> httpx.Response:
        body = next(served, None)
        if body is None:
            return httpx.Response(status, json={"error": "internal"})
        return httpx.Response(200, json=body)

    return handle


def test_a_page_that_fails_mid_listing_fails_the_listing() -> None:
    first = {"items": [{"key": "GR-1"}], "next_cursor": "GR-1"}
    with pytest.raises(SourceUnavailable):
        stub(pages(first, status=500)).list_for_po("receipts", PO)


def test_a_cursor_that_does_not_advance_is_refused() -> None:
    page = {"items": [{"key": "GR-1"}], "next_cursor": "GR-1"}
    with pytest.raises(SourceRecordInvalid, match="did not advance"):
        stub(pages(page, page, page)).list_for_po("receipts", PO)


@pytest.mark.parametrize(
    "body",
    [{"rows": []}, {"items": [{"id": "GR-1"}]}, {"items": "GR-1"}, ["GR-1"]],
    ids=["no items", "no key", "items not a list", "not an object"],
)
def test_a_malformed_listing_is_refused(body: object) -> None:
    with pytest.raises(SourceRecordInvalid):
        stub(pages(body)).list_for_po("receipts", PO)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            httpx.Response(302, headers={"Location": "http://elsewhere/records"}),
            SourceRecordInvalid,
        ),
        (
            httpx.Response(200, content=b"x" * (purchasing.MAX_RESPONSE_BYTES + 1)),
            SourceRecordInvalid,
        ),
        (httpx.Response(503), SourceUnavailable),
        (httpx.Response(403), SourceRecordInvalid),
        (httpx.Response(404), SourceRecordInvalid),
    ],
    ids=["redirect not followed", "oversize", "unavailable", "forbidden", "no such listing"],
)
def test_listing_responses_other_than_a_page_are_errors(
    response: httpx.Response, error: type[Exception]
) -> None:
    with pytest.raises(error):
        stub(lambda request: response).list_for_po("receipts", PO)


def test_a_timeout_is_unavailable_not_absent() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    source = stub(handle)
    with pytest.raises(SourceUnavailable):
        source.fetch("invoices", "INV-1")
    with pytest.raises(SourceUnavailable):
        source.list_for_po("receipts", PO)


def test_a_key_the_source_could_not_have_issued_is_refused_before_any_request() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(SourceRecordInvalid):
        stub(handle).fetch("invoices", "../tasks/T-1")
