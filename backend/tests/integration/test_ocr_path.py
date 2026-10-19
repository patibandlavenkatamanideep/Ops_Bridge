"""OCR through ingestion and an investigation: the same invoice facts that produce a cited
quantity mismatch from a text layer produce only review candidates from OCR, so the case
goes to a person (brief §9: OCR guesses never become authoritative business facts)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from opsbridge.config import REPO_ROOT
from opsbridge.evidence import parsers
from opsbridge.evidence.parsers import parse
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    DocumentVersion,
    EvidenceChunk,
    ExtractedField,
    InvoiceRecord,
    SourceDocument,
)
from opsbridge.simulator.scenarios import Record, load_corpus, render

from .stack import get, investigate, open_case
from .test_api_cases import call
from .test_hardening import tenant_id

pytestmark = pytest.mark.integration


def _invoice_records() -> tuple[Record, Record]:
    """INV-9001 as rendered (text layer) and the same invoice as an image-only PDF."""
    s = next(
        sc
        for sc in load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml").scenarios
        if sc.invoice.number == "INV-9001"
    )
    scanned = s.model_copy(update={"invoice": s.invoice.model_copy(update={"defect": "scanned"})})

    def invoice(sc: Any) -> Record:
        return next(r for r in render(sc) if r[0] == "invoices")

    return invoice(s), invoice(scanned)


def test_ocr_values_are_shown_for_review_but_never_decide(
    stack: dict[str, Any],
    worker_db: Database,
    migrator_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    st = stack
    text_rec, image_rec = _invoice_records()
    transcript = parse(text_rec[2], text_rec[3]).text  # a perfect reading of the same invoice
    real_which = parsers.shutil.which
    monkeypatch.setattr(
        parsers.shutil,
        "which",
        lambda n: "/usr/bin/tesseract" if n == "tesseract" else real_which(n),
    )
    monkeypatch.setattr(parsers, "tesseract_version", lambda: "tesseract test-double")
    monkeypatch.setattr(parsers, "ocr_image", lambda *a, **k: transcript)
    collection, key, _, ctype, po = image_rec
    st["store"].put_record("demo-furniture", collection, key, image_rec[2], ctype, po)
    st["deps"].settings = st["settings"].model_copy(update={"ocr_enabled": True})
    try:
        case_id = open_case(st, "furn-investigator", "demo-furniture", "INV-9001")
        investigate(st, "furn-investigator", "demo-furniture", case_id)
        inv = get(
            st, "furn-investigator", f"/api/v1/tenants/demo-furniture/cases/{case_id}/investigation"
        ).json()
        # The text-layer version of these facts is a quantity mismatch with a follow-up task
        # (test_investigation_stack); from OCR the rules see no invoice and a person decides.
        assert inv["outcome"] in ("manual_review", "needs_information"), inv["outcome"]
        assert not any(f["finding_type"] == "quantity_mismatch" for f in inv["findings"])
        assert inv["recommendation"]["action_type"] != "create_follow_up_task"

        tid = tenant_id(migrator_engine, "demo-furniture")
        with worker_db.session(DbContext(tenant_id=tid)) as s:
            doc = s.execute(
                select(SourceDocument).where(
                    SourceDocument.source_key == f"purchasing:{collection}:{key}"
                )
            ).scalar_one()
            version = s.execute(
                select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
            ).scalar_one()
            assert version.parse_status == "needs_review"
            assert version.parser and version.parser.endswith("+tesseract/test-double")
            assert "not authoritative" in (version.parse_detail or "")
            fields = list(
                s.execute(
                    select(ExtractedField).where(ExtractedField.version_id == version.id)
                ).scalars()
            )
            assert fields and {(f.method, f.status) for f in fields} == {("ocr", "needs_review")}
            number = next(f for f in fields if f.field_path == "invoice.invoice_number")
            assert number.value_text == "INV-9001" and number.locator.startswith("ocr:page=1;line=")
            assert (
                s.execute(
                    select(InvoiceRecord).where(InvoiceRecord.source_version_id == version.id)
                ).first()
                is None
            )
            chunks = list(
                s.execute(
                    select(EvidenceChunk).where(EvidenceChunk.version_id == version.id)
                ).scalars()
            )
            assert chunks and all(c.locator.startswith("ocr:page=1;lines=") for c in chunks)
            assert any("INV-9001" in c.text_content for c in chunks)  # searchable, labelled
        refused = call(
            st["client"],
            st["actors"]["furn-investigator"],
            "POST",
            f"/api/v1/tenants/demo-furniture/cases/{case_id}/fields/{number.id}/corrections",
            json={"new_value": "INV-9001", "reason": "Looks right to me"},
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "ocr_candidate"
    finally:
        st["store"].put_record("demo-furniture", collection, key, text_rec[2], text_rec[3], po)
