"""Reading the purchasing source has one time budget per investigation (production review A15).

Before, each request had its own deadline but the collection as a whole had none: a receipt
listing may follow up to 50 pages and fetch every record, so a slow source held an
investigation for many minutes. Now collection stops when its budget runs out, keeps what it
read, and the investigation goes to manual review instead of concluding from part of the data.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .stack import get, investigate, open_case

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"


def _latest(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    r = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation")
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_a_slow_source_stops_at_the_collection_budget_and_concludes_nothing(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    original = st["deps"].settings
    st["deps"].settings = original.model_copy(update={"source_collection_seconds": 1.0})
    # Each record takes 0.7 s: well inside the per-request timeout, past the budget in total.
    httpx.post(
        f"{st['base']}/_control/faults",
        json={
            "org": "demo-furniture",
            "route": "records.get",
            "mode": "delay",
            "count": 10,
            "delay_ms": 700,
        },
    ).raise_for_status()
    try:
        case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
        investigate(st, "furn-investigator", "demo-furniture", case_id)
    finally:
        st["deps"].settings = original
        st["store"].clear_faults()
    result = _latest(st, case_id)
    assert result["outcome"] == "manual_review", result["findings"]
    assert "source_collection_timed_out" in {f["reason_code"] for f in result["findings"]}


def test_within_the_budget_nothing_changes(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
    investigate(st, "furn-investigator", "demo-furniture", case_id)
    assert _latest(st, case_id)["outcome"] == "discrepancies_found"  # 20 invoiced, 18 received
