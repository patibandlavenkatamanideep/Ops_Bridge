"""Business identifiers as entered: a comparison key never merges distinct documents.

`normalize` trims, upper-cases and collapses inner whitespace. Punctuation and leading zeros
are kept, because INV/123 and INV.123, or INV-0042 and INV-42, can be different documents in
a supplier's or a buyer's system. `similarity_key` folds those differences away; it is used
only to ask a person whether two identifiers name the same document, never as proof that
they do. `similarity_sql` is the same rule as a PostgreSQL expression over a column.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_LEADING_ZEROS = re.compile(r"(^|[^0-9])0+([0-9])")


def normalize(value: str | None) -> str | None:
    if value is None:
        return None
    return _WHITESPACE.sub(" ", value.strip().upper()) or None


def similarity_key(value: str) -> str:
    return _LEADING_ZEROS.sub(r"\1\2", _NON_ALNUM.sub("", value.upper()))


def similarity_sql(column: str) -> str:
    return (
        f"regexp_replace(regexp_replace(upper({column}), '[^A-Z0-9]', '', 'g'), "
        "'(^|[^0-9])0+([0-9])', '\\1\\2', 'g')"
    )
