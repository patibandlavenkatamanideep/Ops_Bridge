"""Purchasing data is read completely, records the source withdrew stop counting, and old
reads cannot be acted on (review Package 11, the source-read contract).

Before: the connector read one response per listing, so a source that pages returned only its
first page and later receipts were silently missing; a receipt deleted, voided or moved to
another PO in the source stayed current in OpsBridge and kept counting as received; and
nothing recorded when the source was read, so a proposal could be approved on data of any
age.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from opsbridge.connectors import purchasing
from opsbridge.persistence.db import Database, DbContext

from .stack import get, investigate, open_case
from .test_api_cases import call
from .test_approvals_execution import _tenant_of, approve, prepared
from .test_hardening import upload

pytestmark = pytest.mark.integration
ORG = "demo-it"
T = f"/api/v1/tenants/{ORG}"
WHO = "it-investigator"
JSON = "application/json"


def receipt(key: str, qty: int, po: str = "PO-2001") -> bytes:
    return json.dumps(
        {
            "grnNumber": key,
            "poRef": po,
            "receivedDate": "2026-09-10",
            "items": [
                {
                    "poLineId": "1",
                    "qtyReceived": str(qty),
                    "uom": "EA",
                    "movementType": "Inbound",
                    "condition": "Accepted",
                }
            ],
        }
    ).encode()


@pytest.fixture
def source(stack) -> Iterator[Any]:  # type: ignore[no-untyped-def]
    """The simulator's store; GRN-6001A (10 of PO-2001's 10) is restored afterwards."""
    store = stack["store"]
    original = store.get_record(ORG, "receipts", "GRN-6001A")
    assert original is not None
    try:
        yield store
    finally:
        store.set_page_limit(ORG, None)
        store.put_record(ORG, "receipts", "GRN-6001A", original[0], original[1], "PO-2001")
        store.delete_record(ORG, "receipts", "GRN-6001B")


def latest(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    r = get(st, WHO, f"{T}/cases/{case_id}/investigation")
    assert r.status_code == 200, r.text
    return dict(r.json())


def codes(found: dict[str, Any]) -> set[str]:
    return {f["reason_code"] for f in found["findings"]}


def split_receipt(store: Any) -> None:
    """PO-2001's 10 units arrive in two receipts, served one per page."""
    store.put_record(ORG, "receipts", "GRN-6001A", receipt("GRN-6001A", 6), JSON, "PO-2001")
    store.put_record(ORG, "receipts", "GRN-6001B", receipt("GRN-6001B", 4), JSON, "PO-2001")
    store.set_page_limit(ORG, 1)


def test_every_page_of_a_listing_is_read(stack, source) -> None:  # type: ignore[no-untyped-def]
    st = stack
    split_receipt(source)
    case_id = open_case(st, WHO, ORG, "INV-7001")
    investigate(st, WHO, ORG, case_id)
    found = latest(st, case_id)
    assert found["outcome"] == "matched", found["findings"]  # 6 + 4 of 10 received


def test_a_listing_cut_off_at_the_page_bound_is_not_concluded(
    stack, source, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    split_receipt(source)
    monkeypatch.setattr(purchasing, "MAX_PAGES", 1)
    case_id = open_case(st, WHO, ORG, "INV-7001")
    investigate(st, WHO, ORG, case_id)
    found = latest(st, case_id)
    assert found["outcome"] == "manual_review", found["findings"]
    assert "source_listing_incomplete" in codes(found)


@pytest.mark.parametrize("how", ["deleted", "moved_to_another_po"])
def test_a_receipt_withdrawn_in_the_source_no_longer_counts(stack, source, how: str) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, WHO, ORG, "INV-7001")
    investigate(st, WHO, ORG, case_id)
    assert latest(st, case_id)["outcome"] == "matched"
    original = source.get_record(ORG, "receipts", "GRN-6001A")

    if how == "deleted":
        assert source.delete_record(ORG, "receipts", "GRN-6001A")
    else:
        moved = receipt("GRN-6001A", 10, po="PO-2999")
        source.put_record(ORG, "receipts", "GRN-6001A", moved, JSON, "PO-2999")
    investigate(st, WHO, ORG, case_id)
    second = latest(st, case_id)
    assert second["version"] == 2
    assert second["outcome"] != "matched", second["findings"]  # nothing received now
    docs = get(st, WHO, f"{T}/cases/{case_id}/documents").json()["items"]
    [grn] = [d for d in docs if d["title"].endswith("GRN-6001A")]
    assert grn["withdrawn_at"] is not None

    # The same record returning to the source counts again.
    source.put_record(ORG, "receipts", "GRN-6001A", original[0], original[1], "PO-2001")
    investigate(st, WHO, ORG, case_id)
    third = latest(st, case_id)
    assert third["outcome"] == "matched", third["findings"]


def test_an_uploaded_receipt_is_not_withdrawn_by_the_source(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, WHO, ORG, "INV-7006")  # the source has no receipt for PO-2006
    investigate(st, WHO, ORG, case_id)
    assert latest(st, case_id)["outcome"] == "needs_information"
    body = receipt("GRN-PAPER-1", 10, po="PO-2006")
    r = upload(
        st["client"], st["actors"][WHO], case_id, "goods_receipt", "grn.json", body, JSON, ORG
    )
    assert r.status_code == 201, r.text
    investigate(st, WHO, ORG, case_id)
    found = latest(st, case_id)
    assert found["outcome"] == "matched", found["findings"]


def test_purchasing_data_older_than_the_limit_cannot_be_acted_on(
    stack, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    tenant = _tenant_of(migrator_engine, "demo-furniture")
    with Database(migrator_engine).session(DbContext(tenant_id=tenant)) as s:
        aged = s.execute(
            text(
                "UPDATE investigation_versions SET evidence_state = jsonb_set(evidence_state, "
                "'{source_valid_until}', '\"2026-01-01T00:00:00+00:00\"') WHERE case_id = :c"
            ),
            {"c": case_id},
        )
        assert aged.rowcount == 1  # type: ignore[attr-defined]
    r: httpx.Response = approve(st, case_id, prop)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_inputs", r.text
    assert "purchasing data was read" in r.text
    furniture = "/api/v1/tenants/demo-furniture"
    shown = get(st, "furn-investigator", f"{furniture}/cases/{case_id}/investigation").json()
    assert "purchasing data was read" in (shown["stale_reason"] or "")
    assert shown["source_read_at"] is not None
    again = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{furniture}/cases/{case_id}/proposals",
        json={},
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "stale_investigation"
