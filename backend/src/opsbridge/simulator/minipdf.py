"""Minimal hand-written PDF writer for synthetic fixtures.

Writes PDF 1.4 syntax directly (catalog, pages, Helvetica text, optional JPEG image
pages) with a correct cross-reference table. It shares no code with the parser under
test (pypdf), so parser tests do not validate a format against its own writer.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    out = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    # Keep to printable Latin-1; replace anything else so the font encoding stays valid.
    return "".join(ch if 32 <= ord(ch) < 127 or 160 <= ord(ch) < 256 else "?" for ch in out)


def _assemble(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def text_pdf(pages: list[list[str]], font_size: int = 9, leading: int = 13) -> bytes:
    """One or more pages of left-aligned text lines."""
    objects: list[bytes] = [b"", b""]  # 1 catalog, 2 pages (filled later)
    font_id = 3
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    page_ids = []
    for lines in pages:
        content = [f"BT /F1 {font_size} Tf {leading} TL 40 800 Td"]
        for i, line in enumerate(lines):
            content.append(("" if i == 0 else "T* ") + f"({_escape(line)}) Tj")
        content.append("ET")
        stream = "\n".join(content).encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    return _assemble(objects)


def image_pdf(jpeg: bytes, width: int, height: int) -> bytes:
    """A single page containing only a JPEG image (no text layer), like a scan."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray "
        f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode()
        + jpeg
        + b"\nendstream",
    ]
    draw = f"q 515 0 0 {int(515 * height / width)} 40 200 cm /Im1 Do Q".encode()
    objects.append(f"<< /Length {len(draw)} >>\nstream\n".encode() + draw + b"\nendstream")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Im1 3 0 R >> >> "
        b"/Contents 4 0 R >>"
    )
    return _assemble(objects)
