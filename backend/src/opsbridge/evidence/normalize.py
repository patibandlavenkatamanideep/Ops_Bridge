"""Text normalization with a named, versioned rule set.

Raw bytes keep their own digest; normalized text gets a separate digest tagged with the
normalization version, so a change to these rules never silently changes what an
existing citation pointed at.
"""

from __future__ import annotations

import unicodedata

from opsbridge.canonical import sha256_bytes

NORMALIZATION_VERSION = "opsbridge-norm/1"


def normalize_text(text: str) -> str:
    """NFC, LF line endings, no trailing whitespace per line, no NULs, single final newline."""
    text = (
        unicodedata.normalize("NFC", text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x00", "")
    )
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def normalized_digest(text: str) -> str:
    return sha256_bytes(normalize_text(text).encode("utf-8"))
