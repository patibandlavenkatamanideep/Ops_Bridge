"""Isolated PDF text extraction. Reads PDF bytes on stdin, writes JSON on stdout.

Run as a subprocess so the parent can enforce a hard timeout and kill it; pypdf never
executes embedded content, but malformed files can be slow to parse.
"""

from __future__ import annotations

import io
import json
import sys


def main() -> int:
    data = sys.stdin.buffer.read()
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 50
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


if __name__ == "__main__":
    sys.exit(main())
