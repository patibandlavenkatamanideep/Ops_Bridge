"""Pattern-based redaction applied before hosted model calls and telemetry.

This reduces exposure of common identifiers; it is NOT guaranteed anonymization.
Names, free-text addresses and context-dependent identifiers can pass through.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

REDACTION_VERSION = "opsbridge-redact/2"  # /2: UUIDs and hyphen-joined ids are never altered

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("us_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Not after a hyphen: digits joined to a prefix (INV-100023456, T-1705292356) are ids.
    (
        "phone",
        re.compile(r"(?<![\w-])\+?\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}(?!\w)"),
    ),
    (
        "bank_account",
        re.compile(
            r"(?i)\b(?:account|acct|routing|sort code)\s*(?:no\.?|number|#)?\s*[:#]?\s*[\d -]{6,20}\b"
        ),
    ),
]


# Opaque record ids (case, operation, attempt) are what logs and traces are meant to carry.
# Digit-only UUID groups look like phone or card numbers to the patterns above, so UUIDs are
# set aside before matching instead of being corrupted at random.
_UUID = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "version": REDACTION_VERSION,
            "counts": self.counts,
            "guarantee": "pattern-based; not anonymization",
        }


def _redact_segment(text: str, counts: Counter[str]) -> str:
    out = text
    for kind, rx in _PATTERNS:

        def repl(m: re.Match[str], kind: str = kind) -> str:
            if kind == "card_number" and not _luhn_ok(m.group(0)):
                return m.group(0)
            counts[kind] += 1
            return f"[REDACTED:{kind}]"

        out = rx.sub(repl, out)
    return out


def redact(text: str) -> RedactionResult:
    counts: Counter[str] = Counter()
    parts: list[str] = []
    last = 0
    for m in _UUID.finditer(text):
        parts.append(_redact_segment(text[last : m.start()], counts))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_redact_segment(text[last:], counts))
    return RedactionResult("".join(parts), dict(counts))
