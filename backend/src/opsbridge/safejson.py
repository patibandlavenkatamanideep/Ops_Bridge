"""JSON from outside OpsBridge, with its nesting depth checked before it is decoded.

`json.loads` recurses once per nesting level, so input nested about a thousand levels deep
raised RecursionError from inside the decoder instead of a decoding error: an upload or
source record failed with an unexpected error, a source listing or task-tracker response
escaped the connector's error handling, and a model answer failed its step instead of being
rejected. The depth is counted in a linear scan first (text inside strings does not count),
and too much nesting is a `TooDeep`, which is a `json.JSONDecodeError` and so a `ValueError`:
every caller that already handles malformed JSON handles it.

An object naming a member twice is a `DuplicateMember` (review N01). The decoder kept the
last value, so an invoice line giving `quantity` as 999999 and then 10 was read as 10 and
matched, while the document a reviewer reads shows both. Names are compared as decoded
(`"qu\u0061ntity"` is `"quantity"`) and in Unicode normalization form C, since two names a
reviewer sees as the same are the same name to them. Which value was meant is not knowable,
so the whole input is refused. OpsBridge's own files (cassettes, ledgers, manifests it
wrote) are read with `json` itself and keep their recorded meaning.
"""

from __future__ import annotations

import json
import re
import unicodedata
from itertools import accumulate
from typing import Any, Protocol

import httpx

MAX_DEPTH = 64  # far beyond any document, record or response OpsBridge reads
_SHOWN = 40  # characters of a duplicate name an error message repeats
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_BRACKETS = re.compile(r"[\[\]{}]")
_STEP = {"[": 1, "{": 1, "]": -1, "}": -1}


class _HasContent(Protocol):
    @property
    def content(self) -> bytes: ...


class TooDeep(json.JSONDecodeError):
    def __init__(self, max_depth: int) -> None:
        super().__init__(f"JSON nesting deeper than {max_depth}", "", 0)


class DuplicateMember(json.JSONDecodeError):
    def __init__(self, name: str) -> None:
        shown = ascii(name[:_SHOWN]) + ("..." if len(name) > _SHOWN else "")
        super().__init__(f"duplicate JSON member {shown}", "", 0)

    def __str__(self) -> str:  # no position: the hook that finds it is not given one
        return self.msg


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for name, _value in pairs:
        key = unicodedata.normalize("NFC", name)
        if key in seen:
            raise DuplicateMember(name)
        seen.add(key)
    return dict(pairs)


def depth(text: str) -> int:
    """The deepest nesting of arrays and objects in `text` (strings skipped)."""
    brackets = _BRACKETS.findall(_STRING.sub("", text))
    return max(accumulate(_STEP[b] for b in brackets), default=0)


def loads(data: str | bytes, *, max_depth: int = MAX_DEPTH, **kwargs: Any) -> Any:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if depth(text) > max_depth:
        raise TooDeep(max_depth)
    return json.loads(text, object_pairs_hook=_unique, **kwargs)


def response_json(r: httpx.Response | _HasContent, *, max_depth: int = MAX_DEPTH) -> Any:
    """`r.json()` with the depth check (an httpx response or a bounded read)."""
    return loads(r.content, max_depth=max_depth)
