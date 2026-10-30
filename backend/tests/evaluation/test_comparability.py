"""A changed grader or frozen plan makes results non-comparable; the warning says it is a
grading change, not a model or rules regression (brief §19)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opsbridge.canonical import digest
from opsbridge.config import REPO_ROOT
from opsbridge.evaluation import manifest
from opsbridge.services import evaluations
from opsbridge.workflow.investigation import WORKFLOW_VERSION

PLAN = REPO_ROOT / "evals" / "manifests" / "opsbridge-bench-v1.json"


def test_changed_scoring_definitions_stop_a_benchmark_run(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = manifest.load(PLAN)
    assert manifest.verify(frozen, REPO_ROOT) == []
    # The code's definition of the plan's frozen scheme changes.
    changed = {**manifest.SCORING, "primary": "outcome_correct: outcome equals the label"}
    monkeypatch.setitem(manifest.SCHEMES, "opsbridge-scoring/1", changed)
    [problem] = manifest.verify(frozen, REPO_ROOT)
    assert problem == manifest.SCORING_CHANGED
    assert "not comparable" in problem and "not evidence of a model or rules change" in problem


def test_runs_from_another_plan_or_grader_are_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = json.loads(PLAN.read_text())
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / PLAN.name).write_text(json.dumps(plan))
    run = {"benchmark_id": plan["benchmark_id"], "manifest_sha256": plan["manifest_sha256"]}
    assert evaluations._comparability(tmp_path, run) == {
        "comparable": True,
        "reasons": [],
        "note": None,
    }
    other = evaluations._comparability(tmp_path, {**run, "manifest_sha256": "sha256:" + "0" * 64})
    assert not other["comparable"] and "different frozen plan" in other["reasons"][0]
    assert "not evidence of a model or rules regression" in (other["note"] or "")
    changed = {**manifest.SCORING, "primary": "changed"}
    monkeypatch.setitem(manifest.SCHEMES, "opsbridge-scoring/1", changed)
    grader = evaluations._comparability(tmp_path, run)
    assert not grader["comparable"] and "scoring definitions differ" in grader["reasons"][0]


def test_a_plan_runs_the_investigation_workflow_it_was_frozen_with() -> None:
    # v1 was frozen before plans named a workflow; its recordings were made with 1.1.0.
    frozen = manifest.load(PLAN)
    assert "workflow_version" not in frozen
    assert manifest.workflow_version_of(frozen) == "investigation/1.1.0"
    fresh = manifest.freeze(REPO_ROOT, "opsbridge-bench-test", modes=["rules_only"], repeats=1)
    assert manifest.workflow_version_of(fresh) == WORKFLOW_VERSION == "investigation/1.2.0"
    assert manifest.verify(fresh, REPO_ROOT) == []
    unknown = {k: v for k, v in fresh.items() if k != "manifest_sha256"}
    unknown["workflow_version"] = "investigation/9.0.0"
    unknown["manifest_sha256"] = digest(unknown)
    [problem] = manifest.verify(unknown, REPO_ROOT)
    assert "investigation/9.0.0" in problem and "cannot be replayed" in problem
