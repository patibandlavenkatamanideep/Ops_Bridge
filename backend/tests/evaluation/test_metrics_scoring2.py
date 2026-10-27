"""Scoring 2: disagreement between repeats reported per axis (production review A03).

Scoring 1 fed binary next-action correctness to EvalSeal's flip rate and called it verdict
instability: three repeats with two different wrong actions counted as stable (all wrong).
Scoring 1 is kept as frozen for the v1 benchmark; scoring 2 reports action, correctness and
outcome/type disagreement and error incidence separately, takes each majority per axis,
and marks a case with missing, duplicate or relabelled repeats incomplete.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from opsbridge import cli
from opsbridge.config import REPO_ROOT
from opsbridge.evaluation import manifest as mf
from opsbridge.evaluation.metrics import acceptance, summarize_mode

TASK = "propose_action:create_follow_up_task"
ESCALATE = "escalate:assign_case"
ASK = "request_information:draft_supplier_message"
S1, S2 = "opsbridge-scoring/1", "opsbridge-scoring/2"
EXPECTED = {"outcome": "discrepancies_found", "types": ["quantity_mismatch"], "next_action": TASK}


def _trial(
    case_id: str,
    repeat: int,
    action: str | None,
    outcome: str = "discrepancies_found",
    types: tuple[str, ...] = ("quantity_mismatch",),
    error: str | None = None,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    e = expected or EXPECTED
    correct = {
        "next_action": action == e["next_action"],
        "outcome": outcome == e["outcome"],
        "types": sorted(types) == sorted(e["types"]),
    }
    correct["case"] = all(correct.values())
    return {
        "case_id": case_id,
        "repeat": repeat,
        "family": "F",
        "template": "t1",
        "pack": "furniture",
        "expected": e,
        "next_action": action,
        "outcome": outcome,
        "types": list(types),
        "error": error,
        "correct": correct,
        "finding_codes": [],
        "explanation_origin": "template",
        "latency_ms": 10,
        "model_calls": 0,
        "model_calls_rejected": 0,
        "tool_calls": [],
        "citations": 0,
        "citations_valid": 0,
        "model_input_tokens": 0,
        "model_output_tokens": 0,
    }


def test_two_different_wrong_actions_are_disagreement_not_stability() -> None:
    # The review's probe: expected a task; got escalate, ask, escalate.
    trials = [_trial("A", 0, ESCALATE), _trial("A", 1, ASK), _trial("A", 2, ESCALATE)]
    v1 = summarize_mode(trials)  # frozen scoring 1, kept as recorded
    assert v1["cases"]["A"]["flip_rate"] == 0 and v1["instability"]["cases_with_any_flip"] == 0
    v2 = summarize_mode(trials, scheme=S2, repeats=3)
    d = v2["cases"]["A"]["disagreement"]
    assert d["action"] == 0.3333 and d["correctness"] == 0.0
    assert v2["instability"]["action"]["cases_with_disagreement"] == 1
    assert v2["instability"]["correctness"]["cases_with_disagreement"] == 0
    assert v2["scoring"] == S2 and v2["complete"] is True


def test_same_action_with_different_findings_is_visible() -> None:
    trials = [
        _trial("A", 0, TASK),
        _trial("A", 1, TASK, types=("price_mismatch",)),
        _trial("A", 2, TASK),
    ]
    d = summarize_mode(trials, scheme=S2, repeats=3)["cases"]["A"]["disagreement"]
    assert d["action"] == 0.0 and d["outcome_types"] == 0.3333


def test_errors_are_counted_and_are_a_verdict_of_their_own() -> None:
    trials = [_trial("A", 0, TASK), _trial("A", 1, None, error="timeout"), _trial("A", 2, TASK)]
    s = summarize_mode(trials, scheme=S2, repeats=3)
    d = s["cases"]["A"]["disagreement"]
    assert d["errors"] == 0.3333 and d["action"] == 0.3333 and d["correctness"] == 0.3333
    assert s["instability"]["errors"]["trials_with_error"] == 1
    assert s["cases"]["A"]["correct"]["next_action"] is True  # majority TASK, 2 of 3


def test_each_majority_is_taken_on_its_own_axis() -> None:
    # Scoring 1 took the outcome and types of the first trial with the majority action.
    trials = [
        _trial("A", 0, TASK, outcome="manual_review", types=()),
        _trial("A", 1, TASK),
        _trial("A", 2, ESCALATE),
    ]
    v1 = summarize_mode(trials)["cases"]["A"]
    v2 = summarize_mode(trials, scheme=S2, repeats=3)["cases"]["A"]
    assert v1["majority"]["outcome"] == "manual_review"
    assert v2["majority"]["outcome"] == "discrepancies_found"
    assert v2["majority"]["types"] == ["quantity_mismatch"]
    assert v2["correct"]["outcome"] is True
    # No single repeat had action, outcome and types all as the majorities: three distinct
    # whole verdicts tie, the earliest is reported, and the case is not counted correct.
    assert v2["whole_verdict"]["tied"] is True and v2["whole_verdict"]["count"] == 1
    assert v2["correct"]["case"] is False


def test_missing_duplicate_or_relabelled_repeats_are_incomplete() -> None:
    other = {**EXPECTED, "next_action": ESCALATE}
    trials = [
        _trial("missing", 0, TASK),
        _trial("missing", 2, TASK),
        _trial("duplicate", 0, TASK),
        _trial("duplicate", 0, TASK),
        _trial("duplicate", 1, TASK),
        _trial("relabelled", 0, TASK),
        _trial("relabelled", 1, TASK, expected=other),
        _trial("relabelled", 2, TASK),
        *[_trial("whole", r, TASK) for r in range(3)],
    ]
    s = summarize_mode(trials, scheme=S2, repeats=3)
    assert s["complete"] is False
    assert s["incomplete_cases"] == ["duplicate", "missing", "relabelled"]
    problems = {cid: " ".join(s["cases"][cid]["problems"]) for cid in s["incomplete_cases"]}
    assert "missing repeat 1" in problems["missing"]
    assert "repeat 0 recorded 2 times" in problems["duplicate"]
    assert "labels differ" in problems["relabelled"]
    assert s["cases"]["whole"]["complete"] is True
    summary = {"modes": {"rules_only": s}}
    policy = json.loads((REPO_ROOT / "evals" / "acceptance-policy.json").read_text())
    [gate] = [g for g in acceptance(summary, policy, "heldout") if g["mode"] == "rules_only"]
    assert gate["status"] == "incomplete"


def test_the_frozen_v1_plan_still_verifies_and_new_plans_use_scoring_2() -> None:
    frozen = mf.load(REPO_ROOT / "evals" / "manifests" / "opsbridge-bench-v1.json")
    assert mf.scheme_of(frozen["scoring"]) == S1
    assert mf.verify(frozen, REPO_ROOT) == []
    assert mf.SCHEMES[S1] == frozen["scoring"]  # scoring 1 is unchanged, byte for byte
    fresh = mf.freeze(REPO_ROOT, "opsbridge-bench-test", modes=["rules_only"], repeats=3)
    assert mf.scheme_of(fresh["scoring"]) == S2


def test_a_recorded_run_can_be_read_under_scoring_2_without_changing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = REPO_ROOT / "evals" / "runs" / "opsbridge-bench-v1" / "heldout-record-20261015T022744Z"
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
    assert cli.main(["eval-rescore", "--run", str(run)]) == 0
    out = capsys.readouterr().out
    assert "recorded under opsbridge-scoring/1, read under opsbridge-scoring/2" in out
    assert "derived; not the run's result" in out
    # The recorded rules and mock trials are deterministic: no disagreement on any axis.
    assert "action 0/40, correctness 0/40, outcome_types 0/40" in out
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()} == before
