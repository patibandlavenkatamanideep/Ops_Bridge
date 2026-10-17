"""Canonical JSON and digests.

Scheme "opsbridge-cjson/1": UTF-8 JSON with sorted keys, no insignificant whitespace,
ensure_ascii=False, Decimals rendered as plain strings. Digests are "sha256:<hex>".
This matches the convention EvalSeal documents for its agent artifacts
(json.dumps(sort_keys=True, separators=(",", ":"))) for ASCII content.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

CANONICALIZATION = "opsbridge-cjson/1"


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"not canonicalizable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    if isinstance(value, float):
        raise TypeError("floats are not allowed in canonical payloads")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    _reject_floats(value)
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("floats are not allowed in canonical payloads")
    if isinstance(value, dict):
        for v in value.values():
            _reject_floats(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_floats(v)
