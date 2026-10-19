"""The OCR path with the real tesseract engine on the rasterized fixture. Skipped where
tesseract is not installed, except with OPSBRIDGE_REQUIRE_OCR=1 (hosted CI), where its
absence fails instead of skipping."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

import pytest

from opsbridge.evidence.parsers import OcrSettings, parse
from opsbridge.mapping import DocRef, map_ocr_candidates
from opsbridge.packs import PackRegistry

pytestmark = pytest.mark.ocr

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "fixtures" / "documents"
MIN_TOKEN_RECALL = 0.9


def _require_tesseract() -> None:
    if shutil.which("tesseract") is None:
        if os.environ.get("OPSBRIDGE_REQUIRE_OCR") == "1":
            pytest.fail("OPSBRIDGE_REQUIRE_OCR=1 but tesseract is not installed")
        pytest.skip("tesseract not installed (runs in hosted CI and the app container)")


def _tokens(text: str) -> list[str]:
    # Table separators are drawn as "|", which OCR reads as I, l or |; they carry no fact.
    return [t for t in re.split(r"\s+", text) if t and t != "|"]


def test_real_ocr_reads_the_raster_but_its_values_stay_candidates() -> None:
    _require_tesseract()
    doc = parse(
        (DOCS / "rasterized_invoice.pdf").read_bytes(), "application/pdf", OcrSettings(True)
    )
    assert doc.status == "needs_review"
    assert doc.ocr_engine and doc.ocr_engine.startswith("tesseract")
    expected = _tokens((DOCS / "cupsfilter_text.txt").read_text())
    got = set(_tokens(doc.text))
    recall = sum(t in got for t in expected) / len(expected)
    print(f"{doc.ocr_engine}: token recall {recall:.3f} over {len(expected)} tokens")
    assert recall >= MIN_TOKEN_RECALL, (recall, doc.text)
    for fact in ("INV-CUPS-1", "PO-CUPS-1", "3000.00"):
        assert fact in doc.text
    pack = PackRegistry(REPO / "packs").get("furniture")
    ref = DocRef(str(uuid.uuid4()), str(uuid.uuid4()), "sha256:" + "0" * 64)
    candidates = map_ocr_candidates(pack, "invoice", doc, ref)
    assert candidates.invoices == []  # never a record, however good the reading
    number = [f for f in candidates.fields if f.field_path == "invoice.invoice_number"]
    assert number and number[0].value == "INV-CUPS-1" and number[0].status == "needs_review"
