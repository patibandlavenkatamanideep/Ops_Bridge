"""A newer source document that cannot be read must not leave the old facts current
(review finding F12).

Records extracted from an earlier version stayed current when the newest version was
rejected, needed review, had text only from OCR, or was refused as an unsupported type, so
matching kept using facts the source system no longer showed. The case now goes to manual
review until a readable version arrives; the earlier extraction stays in the history.
"""

from __future__ import annotations

from typing import Any

import pytest

from opsbridge.config import REPO_ROOT

from .stack import get, investigate, open_case

pytestmark = pytest.mark.integration
ORG = "demo-furniture"
T = f"/api/v1/tenants/{ORG}"
FIXTURES = REPO_ROOT / "fixtures" / "documents"


def latest(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    r = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation")
    assert r.status_code == 200, r.text
    return dict(r.json())


@pytest.mark.parametrize(
    ("replacement", "ctype", "reason"),
    [
        ("malformed_truncated.pdf", "application/pdf", "invoice_extraction_needs_review"),
        ("rasterized_invoice.pdf", "application/pdf", "invoice_extraction_needs_review"),
        (None, "text/html", "source_document_unreadable"),
    ],
)
def test_an_unreadable_replacement_invoice_is_not_matched_on_the_old_extraction(
    stack, replacement: str | None, ctype: str, reason: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = open_case(st, "furn-investigator", ORG, "INV-9001")
    investigate(st, "furn-investigator", ORG, case_id)
    first = latest(st, case_id)
    assert first["outcome"] == "discrepancies_found"  # 20 invoiced, 18 received

    store = st["store"]
    original = store.get_record(ORG, "invoices", "INV-9001")
    assert original is not None
    body = (FIXTURES / replacement).read_bytes() if replacement else b"<html>INV-9001</html>"
    store.put_record(ORG, "invoices", "INV-9001", body, ctype, "PO-1001")
    try:
        investigate(st, "furn-investigator", ORG, case_id)
    finally:
        store.put_record(ORG, "invoices", "INV-9001", original[0], original[1], "PO-1001")
    second = latest(st, case_id)
    assert second["version"] == 2
    assert second["outcome"] == "manual_review", second["findings"]
    codes = {f["reason_code"] for f in second["findings"]}
    assert reason in codes and "quantity_mismatch" not in {
        f["finding_type"] for f in second["findings"]
    }
