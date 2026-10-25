"""Corrections must change what matching uses, or be refused (review finding F11).

Matching applies corrections to a fixed set of fields. A correction to any other field used
to be accepted and marked "corrected" while matching kept the extracted value.
"""

from __future__ import annotations

from typing import Any

import pytest

from opsbridge.evidence.records import CORRECTABLE_FIELDS

from .stack import get, investigate, open_case
from .test_api_cases import call

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"


def invoice_fields(st: dict[str, Any], case_id: str) -> list[dict[str, Any]]:
    docs = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents").json()["items"]
    inv = next(d for d in docs if d["kind"] == "invoice")
    url = f"{T}/cases/{case_id}/documents/versions/{inv['versions'][-1]['version_id']}"
    return list(get(st, "furn-investigator", url).json()["fields"])


def test_only_fields_matching_uses_can_be_corrected(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
    investigate(st, "furn-investigator", "demo-furniture", case_id)
    fields = invoice_fields(st, case_id)
    flags = {f["field_path"]: f["correctable"] for f in fields}
    assert flags["invoice_line.quantity"] is True and flags["invoice.currency"] is False
    assert {p for p, ok in flags.items() if ok} <= CORRECTABLE_FIELDS

    currency = next(f for f in fields if f["field_path"] == "invoice.currency")
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/fields/{currency['field_id']}/corrections",
        json={"new_value": "EUR", "reason": "Supplier invoiced in euros"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_correctable", r.text
    after = next(f for f in invoice_fields(st, case_id) if f["field_path"] == "invoice.currency")
    assert after["status"] != "corrected" and after["current_value"] == currency["original_value"]
