"""A replay must reproduce its recorded run case by case; any verdict difference is reported."""

from __future__ import annotations

from typing import Any

from opsbridge.evaluation.reproduce import differences


def _modes(**cases: list[str]) -> dict[str, Any]:
    return {"cases": {cid: {"verdicts": v} for cid, v in cases.items()}}


def test_identical_verdicts_reproduce_and_any_difference_is_named() -> None:
    recorded = {
        "modes": {"rules_only": _modes(a=["x", "x"], b=["y", "y"]), "mock": _modes(a=["x"])}
    }
    same = {"rules_only": _modes(a=["x", "x"], b=["y", "y"]), "mock": _modes(a=["x"])}
    assert differences(recorded, same) == []
    changed = {"rules_only": _modes(a=["x", "error:Quota"], b=["y", "y"])}
    assert differences(recorded, changed) == [
        "mock: not replayed",
        "rules_only a: recorded ['x', 'x'] → replay ['x', 'error:Quota']",
    ]
    assert differences(recorded, {"rules_only": _modes(a=["x", "x"]), "mock": _modes(a=["x"])}) == [
        "rules_only b: recorded ['y', 'y'] → replay None"
    ]
