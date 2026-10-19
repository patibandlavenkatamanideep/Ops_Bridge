"""Canary regression suite: the pinned baseline matches the code; any change is reported."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from opsbridge.config import REPO_ROOT
from opsbridge.evaluation import canary


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return canary.run(REPO_ROOT)


def test_current_rules_match_the_pinned_canary_baseline(current: dict[str, Any]) -> None:
    baseline = canary.load(REPO_ROOT / "evals" / "baselines" / "canary-rules.json")
    assert canary.compare(baseline, current) == []
    assert "heldout" not in current["splits"]


def test_canary_reports_regressions_and_improvements_alike(current: dict[str, Any]) -> None:
    changed = copy.deepcopy(current)
    case_id = sorted(changed["cases"])[0]
    changed["cases"][case_id]["next_action"] = "escalate:assign_case"
    del changed["cases"][sorted(changed["cases"])[-1]]
    diffs = canary.compare(current, changed)
    assert len(diffs) == 2 and diffs[0].startswith(case_id) and "missing" in diffs[1]
