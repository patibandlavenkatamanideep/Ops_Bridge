"""JSON from outside OpsBridge, with its nesting depth checked before it is decoded.

`json.loads` recurses once per nesting level, so input nested about a thousand levels deep
raised RecursionError from inside the decoder instead of a decoding error: an upload or
source record failed with an unexpected error, a source listing or task-tracker response
escaped the connector's error handling, and a model answer failed its step instead of being
rejected. The depth is counted in a linear scan first (text inside strings does not count),
and too much nesting is a `TooDeep`, which is a `json.JSONDecodeError` and so a `ValueError`:
every caller that already handles malformed JSON handles it.
"""

from __future__ import annotations

import json
import re
from itertools import accumulate
from typing import Any, Protocol

import httpx

MAX_DEPTH = 64  # far beyond any document, record or response OpsBridge reads
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_BRACKETS = re.compile(r"[\[\]{}]")
_STEP = {"[": 1, "{": 1, "]": -1, "}": -1}


class _HasContent(Protocol):
    @property
    def content(self) -> bytes: ...


class TooDeep(json.JSONDecodeError):
    def __init__(self, max_depth: int) -> None:
        super().__init__(f"JSON nesting deeper than {max_depth}", "", 0)


def depth(text: str) -> int:
    """The deepest nesting of arrays and objects in `text` (strings skipped)."""
    brackets = _BRACKETS.findall(_STRING.sub("", text))
    return max(accumulate(_STEP[b] for b in brackets), default=0)


def loads(data: str | bytes, *, max_depth: int = MAX_DEPTH, **kwargs: Any) -> Any:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if depth(text) > max_depth:
        raise TooDeep(max_depth)
    return json.loads(text, **kwargs)


def response_json(r: httpx.Response | _HasContent, *, max_depth: int = MAX_DEPTH) -> Any:
    """`r.json()` with the depth check (an httpx response or a bounded read)."""
    return loads(r.content, max_depth=max_depth)
