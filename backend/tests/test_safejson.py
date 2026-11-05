"""JSON from outside OpsBridge (review Package 9: deeply nested JSON handled predictably;
review N01: an object naming a member twice is refused, not read as its last value)."""

from __future__ import annotations

import json
from decimal import Decimal

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


@pytest.mark.parametrize(
    "text",
    [
        '{"quantity": 999999, "quantity": 10}',
        '{"quantity": 10, "quantity": 10}',  # the same value twice is still two members
        '{"invoice": {"lines": [{"ok": 1}, {"qty": 1, "uom": "EA", "qty": 3}]}}',
        '[{"a": 1}, {"a": 1, "b": {"c": 1, "c": 2}}]',
        '{"qu\\u0061ntity": 999999, "quantity": 10}',  # an escape spelling the same name
        '{"quantit\\u00e9": 1, "quantite\\u0301": 2}',  # composed and decomposed é
    ],
)
def test_an_object_naming_a_member_twice_is_refused(text: str) -> None:
    with pytest.raises(safejson.DuplicateMember) as exc:
        safejson.loads(text)
    # Malformed JSON to every caller that handles it, and never one of the two values.
    assert isinstance(exc.value, json.JSONDecodeError) and isinstance(exc.value, ValueError)
    with pytest.raises(ValueError):
        safejson.response_json(httpx.Response(200, content=text.encode()))


def test_the_refusal_names_the_member_briefly_and_printably() -> None:
    name = "\x1b[2J‮" + "k" * 100_000
    text = "{" + f"{json.dumps(name)}: 1, {json.dumps(name)}: 2" + "}"
    with pytest.raises(safejson.DuplicateMember) as exc:
        safejson.loads(text)
    message = str(exc.value)
    assert len(message) < 200 and message.isprintable() and "\x1b" not in message
    assert "duplicate JSON member" in message


def test_distinct_members_and_the_number_hooks_are_unchanged() -> None:
    text = '{"a": {"x": 1}, "b": [{"x": 2}, {"x": 3}], "A": 1.50, "quantité": "1"}'
    assert safejson.loads(text, parse_float=Decimal) == {
        "a": {"x": 1},
        "b": [{"x": 2}, {"x": 3}],
        "A": Decimal("1.50"),  # names differing in case are different members
        "quantité": "1",
    }
