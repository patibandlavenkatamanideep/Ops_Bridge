"""A repeated investigation names why the invoice could not be read (production review A13).

Before (`investigation/1.1.0`): the reason came from the problems of the current read, and a
document whose bytes had not changed is not read again, so a second investigation said "The
invoice document could not be read reliably ()" (and a reason found by the parser, such as a
missing text layer, was never named). Now (`investigation/1.2.0`) the finding names the reason
stored with the document's newest version, on every run. Each run records the workflow
version it ran; 1.1.0 is kept only to replay recordings made with it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from opsbridge.workflow.investigation import WORKFLOW_VERSION

from .stack import get, investigate, open_case

pytestmark = pytest.mark.integration
ORG = "demo-it"
T = f"/api/v1/tenants/{ORG}"
WHO = "it-investigator"


def _finding(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    r = get(st, WHO, f"{T}/cases/{case_id}/investigation")
    assert r.status_code == 200, r.text
    found = [
        f for f in r.json()["findings"] if f["reason_code"] == "invoice_extraction_needs_review"
    ]
    assert len(found) == 1, r.json()["findings"]
    return dict(found[0])


def test_a_repeated_investigation_keeps_the_reason_an_invoice_could_not_be_read(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    store = st["store"]
    original = store.get_record(ORG, "invoices", "INV-7001")
    assert original is not None
    unreadable = json.dumps(json.loads(original[0]) | {"items": "10 laptops"}).encode()
    store.put_record(ORG, "invoices", "INV-7001", unreadable, original[1], "PO-2001")
    try:
        case_id = open_case(st, WHO, ORG, "INV-7001")
        runs, findings = [], []
        for _ in range(2):  # the second reads the same bytes: no new document version
            runs.append(investigate(st, WHO, ORG, case_id))
            findings.append(_finding(st, case_id))
    finally:
        store.put_record(ORG, "invoices", "INV-7001", original[0], original[1], "PO-2001")
    assert WORKFLOW_VERSION == "investigation/1.2.0"
    assert [r["workflow_version"] for r in runs] == [WORKFLOW_VERSION] * 2
    first, second = (f["summary"] for f in findings)
    assert second == first
    assert "()" not in second
    reason = second.split("invoice INV-7001: ", 1)[1].split(")", 1)[0]  # document: its reason
    assert reason.strip(), second
