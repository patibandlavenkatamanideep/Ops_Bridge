"""Decimal parsing and currency handling.

Monetary and quantity values enter the domain as strings (or Decimal), never as
binary floats. Parsing is strict: floats, NaN, infinities and exponent-heavy
inputs are rejected so a malformed source value cannot silently become a number.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, InvalidOperation

# Decimal context with ample precision; quantization is always explicit.
DECIMAL_CONTEXT = Context(prec=34)

_DECIMAL_RE = re.compile(r"^[+-]?(\d{1,18})(\.\d{1,12})?$")

ROUNDING_MODES = {
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_HALF_UP": ROUND_HALF_UP,
}

# ISO 4217 minor units for the currencies OpsBridge knows how to display and round.
# A currency outside this table is "unsupported" and routes to manual review.
CURRENCY_MINOR_UNITS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CAD": 2,
    "AUD": 2,
    "INR": 2,
    "JPY": 0,
}


class DecimalParseError(ValueError):
    """Raised when a value cannot be parsed as an exact decimal."""


def parse_decimal(value: object) -> Decimal:
    """Parse an exact decimal from a string, int or Decimal.

    Floats are rejected on purpose: by the time a float exists, the exact source
    value may already be lost.
    """
    if isinstance(value, bool):
        raise DecimalParseError("booleans are not numbers")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DecimalParseError("non-finite decimal")
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise DecimalParseError("binary floating-point values are not accepted; send a string")
    if not isinstance(value, str):
        raise DecimalParseError(f"unsupported type {type(value).__name__}")
    text = value.strip().replace(",", "") if _looks_grouped(value.strip()) else value.strip()
    if not _DECIMAL_RE.match(text):
        raise DecimalParseError(f"not a plain decimal: {value!r}")
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise DecimalParseError(str(exc)) from exc


def _looks_grouped(text: str) -> bool:
    """Accept thousands separators only in the strict 1,234,567.89 form."""
    return bool(re.match(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$", text))


def decimal_str(value: Decimal) -> str:
    """Canonical string form for JSON: no exponent, no trailing-zero stripping."""
    return format(value, "f")


def minor_units(currency: str) -> int | None:
    return CURRENCY_MINOR_UNITS.get(currency.upper())


def quantize(value: Decimal, places: int, rounding: str = "ROUND_HALF_EVEN") -> Decimal:
    if rounding not in ROUNDING_MODES:
        raise ValueError(f"unknown rounding mode {rounding}")
    exponent = Decimal(1).scaleb(-places)
    return value.quantize(exponent, rounding=ROUNDING_MODES[rounding], context=DECIMAL_CONTEXT)


def one_minor_unit(currency: str) -> Decimal:
    places = minor_units(currency)
    if places is None:
        raise ValueError(f"unsupported currency {currency}")
    return Decimal(1).scaleb(-places)
