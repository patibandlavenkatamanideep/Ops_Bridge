"""Isolated PDF text extraction. Reads PDF bytes on stdin, writes JSON on stdout.

Run as a subprocess so the parent can enforce a hard timeout and kill it; pypdf never
executes embedded content, but malformed files can be slow to parse.

`pdf_worker MAX_PAGES images N` instead returns the embedded images of the first N pages as
grayscale PNGs (base64) for the optional OCR path. Images are decoded here, inside the
subprocess, with a pixel limit, so a hostile image cannot stall or exhaust the parent.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import warnings

MAX_IMAGE_PIXELS = 40_000_000  # about 300 dpi for an A3 page
MAX_IMAGES_PER_PAGE = 4
# A file of under 1 MB whose streams inflate to text made this process use 850 MB before the
# timeout stopped it; several at once could exhaust the container.
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024


def _limit_memory() -> None:
    """Cap this process's address space where the OS enforces it (Linux: the container and
    CI). Past the cap, parsing fails with MemoryError and the file is rejected. macOS refuses
    lower limits; there only the parent's timeout applies."""
    if not sys.platform.startswith("linux"):
        return
    import resource

    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = MEMORY_LIMIT_BYTES if hard == resource.RLIM_INFINITY else min(MEMORY_LIMIT_BYTES, hard)
    resource.setrlimit(resource.RLIMIT_AS, (cap, hard))


def page_images(reader: object, pages: int) -> tuple[list[list[object]], list[str]]:
    """[page, base64 PNG] for each embedded image of the first `pages` pages, plus problems."""
    from PIL import Image

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            return _decode(reader, pages)
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def _decode(reader: object, pages: int) -> tuple[list[list[object]], list[str]]:
    found: list[list[object]] = []
    problems: list[str] = []
    for page_no, page in enumerate(reader.pages[:pages], start=1):  # type: ignore[attr-defined]
        try:
            images = list(page.images)
        except Exception as exc:
            problems.append(f"page {page_no}: images unreadable ({type(exc).__name__})")
            continue
        if len(images) > MAX_IMAGES_PER_PAGE:
            problems.append(f"page {page_no}: only the first {MAX_IMAGES_PER_PAGE} images read")
        for img in images[:MAX_IMAGES_PER_PAGE]:
            try:
                pil = img.image
                if pil is None:
                    raise ValueError("no decoder")
                if pil.width * pil.height > MAX_IMAGE_PIXELS:
                    raise ValueError("too many pixels")
                buf = io.BytesIO()
                pil.convert("L").save(buf, format="PNG")
            except Exception as exc:
                problems.append(f"page {page_no}: image not decoded ({type(exc).__name__})")
                continue
            found.append([page_no, base64.b64encode(buf.getvalue()).decode("ascii")])
    return found, problems


def main() -> int:
    _limit_memory()
    data = sys.stdin.buffer.read()
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    if len(sys.argv) > 3 and sys.argv[2] == "images":
        return _images(data, max_pages, int(sys.argv[3]))
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            out = {
                "status": "rejected",
                "detail": "encrypted PDF",
                "page_count": 0,
                "pages": [],
                "images": 0,
            }
        elif len(reader.pages) > max_pages:
            out = {
                "status": "rejected",
                "detail": f"more than {max_pages} pages",
                "page_count": len(reader.pages),
                "pages": [],
                "images": 0,
            }
        else:
            pages = [[i + 1, page.extract_text() or ""] for i, page in enumerate(reader.pages)]
            images = 0
            for page in reader.pages:
                try:
                    images += len(page.images)
                except Exception:  # noqa: S112 - informational only
                    continue
            out = {
                "status": "ok",
                "detail": None,
                "page_count": len(pages),
                "pages": pages,
                "images": images,
            }
    except MemoryError:
        out = {
            "status": "rejected",
            "detail": "PDF needs more memory than the parser may use",
            "page_count": 0,
            "pages": [],
            "images": 0,
        }
    except Exception as exc:  # malformed PDFs raise many exception types
        out = {
            "status": "rejected",
            "detail": f"malformed PDF: {type(exc).__name__}",
            "page_count": 0,
            "pages": [],
            "images": 0,
        }
    sys.stdout.write(json.dumps(out))
    return 0


def _images(data: bytes, max_pages: int, ocr_pages: int) -> int:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted or len(reader.pages) > max_pages:
            out: dict[str, object] = {"status": "rejected", "images": [], "problems": []}
        else:
            found, problems = page_images(reader, ocr_pages)
            out = {"status": "ok", "images": found, "problems": problems}
    except Exception as exc:  # malformed PDFs raise many exception types
        out = {"status": "rejected", "images": [], "problems": [type(exc).__name__]}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
