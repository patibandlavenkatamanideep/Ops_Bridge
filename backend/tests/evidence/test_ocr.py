"""Optional OCR path for PDFs without a text layer: off by default, bounded, and never
authoritative (brief §9). Tesseract itself is replaced by a test double here; the real
engine is exercised in test_ocr_tesseract.py."""

from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from opsbridge.evidence import parsers
from opsbridge.evidence.parsers import OcrSettings, ocr_unavailable_reason, parse
from opsbridge.evidence.pdf_worker import MAX_IMAGE_PIXELS, page_images
from opsbridge.mapping import DocRef, map_document, map_ocr_candidates
from opsbridge.packs import PackRegistry
from opsbridge.simulator.minipdf import image_pdf

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "fixtures" / "documents"
RASTER = (DOCS / "rasterized_invoice.pdf").read_bytes()
# The hand-written text the raster was rendered from: a perfect transcription.
TRANSCRIPT = (DOCS / "cupsfilter_text.txt").read_text()
ON = OcrSettings(enabled=True)


def _png(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


@pytest.fixture
def fake_tesseract(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    seen: list[bytes] = []
    real_which = parsers.shutil.which
    monkeypatch.setattr(
        parsers.shutil,
        "which",
        lambda n: "/usr/bin/tesseract" if n == "tesseract" else real_which(n),
    )
    monkeypatch.setattr(parsers, "tesseract_version", lambda: "tesseract test-double")

    def ocr(image: bytes, timeout: float = 15.0, language: str = "eng") -> str:
        seen.append(image)
        return TRANSCRIPT

    monkeypatch.setattr(parsers, "ocr_image", ocr)
    return seen


def test_worker_returns_the_page_image_as_grayscale_png() -> None:
    images, problems = page_images(PdfReader(io.BytesIO(RASTER)), 3)
    assert problems == []
    [(page, b64)] = images
    img = _png(str(b64))
    assert page == 1 and img.format == "PNG" and img.mode == "L"
    assert img.size == (1700, 2200)  # as rendered (sips reported 1700 x 2200)


def test_a_fax_style_one_bit_image_round_trips_pixel_for_pixel() -> None:
    src = Image.new("1", (64, 32), 1)
    for x in range(10, 54):
        src.putpixel((x, 16), 0)  # one black line
    buf = io.BytesIO()
    src.save(buf, format="PDF")  # Pillow as an independent PDF producer
    [(_, b64)], problems = page_images(PdfReader(io.BytesIO(buf.getvalue())), 1)
    assert problems == []
    out = _png(str(b64))
    assert out.size == src.size
    assert out.tobytes() == src.convert("L").tobytes()


def test_an_oversized_image_is_refused_not_decoded() -> None:
    side = int(MAX_IMAGE_PIXELS**0.5) + 100
    buf = io.BytesIO()
    Image.new("L", (side, side), 255).save(buf, format="JPEG")
    images, problems = page_images(PdfReader(io.BytesIO(image_pdf(buf.getvalue(), side, side))), 1)
    assert images == []
    assert problems and "DecompressionBomb" in problems[0]  # refused, never decoded
    assert Image.MAX_IMAGE_PIXELS != MAX_IMAGE_PIXELS  # the limit does not leak


def test_ocr_is_off_by_default_and_no_text_is_invented() -> None:
    doc = parse(RASTER, "application/pdf")
    assert doc.status == "needs_review" and doc.ocr_engine is None
    assert "no extractable text layer" in (doc.detail or "") and "OCR is not enabled" in (
        doc.detail or ""
    )
    assert doc.text.strip() == "" and doc.lines == []


def test_enabled_without_tesseract_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsers.shutil, "which", lambda n: None)
    doc = parse(RASTER, "application/pdf", ON)
    assert doc.status == "needs_review" and "tesseract is not installed" in (doc.detail or "")
    assert doc.lines == []


def test_language_codes_are_validated_before_reaching_the_command_line() -> None:
    assert "not valid" in (ocr_unavailable_reason(OcrSettings(True, language="eng -c x=1")) or "")


def test_ocr_text_is_labelled_and_the_document_still_needs_review(
    fake_tesseract: list[bytes],
) -> None:
    doc = parse(RASTER, "application/pdf", ON)
    assert len(fake_tesseract) == 1  # one page image went to the engine
    assert doc.status == "needs_review"  # never "parsed"
    assert doc.ocr_engine == "tesseract test-double"
    assert doc.parser.endswith("+tesseract/test-double")
    assert "ocr_text_not_authoritative" in doc.flags
    assert "not authoritative" in (doc.detail or "")
    assert all(line.origin == "ocr" for line in doc.lines)
    by_text = {line.text: line.locator for line in doc.lines}
    assert by_text["Invoice No: INV-CUPS-1"] == "ocr:page=1;line=2"


def test_pages_beyond_the_limit_are_not_read_and_the_detail_says_so(
    fake_tesseract: list[bytes],
) -> None:
    pages = [Image.new("L", (200, 100), 255) for _ in range(2)]
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    doc = parse(buf.getvalue(), "application/pdf", OcrSettings(True, max_pages=1))
    assert len(fake_tesseract) == 1 and doc.page_count == 2
    assert {line.page for line in doc.lines} == {1}
    assert "1 later page(s) not read (OCR page limit 1)" in (doc.detail or "")


def test_ocr_values_are_candidates_only_never_records(fake_tesseract: list[bytes]) -> None:
    doc = parse(RASTER, "application/pdf", ON)
    ref = DocRef(str(uuid.uuid4()), str(uuid.uuid4()), "sha256:" + "0" * 64)
    pack = PackRegistry(REPO / "packs").get("furniture")
    refused = map_document(pack, "invoice", doc, ref)
    assert refused.invoices == [] and "document not parsed" in refused.problems[0]
    candidates = map_ocr_candidates(pack, "invoice", doc, ref)
    assert (
        candidates.invoices == [] and candidates.receipts == [] and candidates.purchase_orders == []
    )
    assert candidates.fields and all(
        f.method == "ocr"
        and f.status == "needs_review"
        and "not used by the matching rules" in (f.note or "")
        for f in candidates.fields
    )
    number = next(f for f in candidates.fields if f.field_path == "invoice.invoice_number")
    assert number.value == "INV-CUPS-1" and number.locator == "ocr:page=1;line=2"
