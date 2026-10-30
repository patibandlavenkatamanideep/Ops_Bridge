"""Depth-checked JSON decoding (review Package 9: deeply nested JSON handled predictably)."""

from __future__ import annotations

import json

import httpx
import pytest

from opsbridge import safejson


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", 0),
        ("[]", 1),
        ('[{"a": [1, {"b": []}]}]', 5),
        ('{"s": "[[[[{{{{"}', 1),  # brackets inside strings are text
        ('{"s": "a \\" [[[["}', 1),  # an escaped quote does not end the string
        ("[" * 100_000 + "]" * 100_000, 100_000),
    ],
)
def test_depth_counts_nesting_not_text(text: str, expected: int) -> None:
    assert safejson.depth(text) == expected


def test_too_deep_is_refused_before_decoding_as_malformed_json() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    with pytest.raises(safejson.TooDeep) as exc:
        safejson.loads(deep)
    assert isinstance(exc.value, json.JSONDecodeError) and isinstance(exc.value, ValueError)
    assert safejson.loads(b'{"a": [1, 2]}') == {"a": [1, 2]}
    with pytest.raises(safejson.TooDeep):
        safejson.loads("[[[1]]]", max_depth=2)


def test_responses_are_decoded_with_the_same_limit() -> None:
    ok = httpx.Response(200, json={"items": [{"key": "GR-1"}]})
    assert safejson.response_json(ok) == {"items": [{"key": "GR-1"}]}
    with pytest.raises(ValueError):
        safejson.response_json(httpx.Response(200, content=b"[" * 5000 + b"]" * 5000))
