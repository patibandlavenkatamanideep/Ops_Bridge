"""Repeated runs that disagree: instability is reported per case with what the repeat count
cannot show (brief §19: empirical variance with its sampling limits)."""

from __future__ import annotations

from typing import Any

from opsbridge.evaluation.metrics import summarize_mode

TASK = "propose_action:create_follow_up_task"
ESCALATE = "escalate:assign_case"


def _trial(case_id: str, repeat: int, action: str) -> dict[str, Any]:
    right = action == TASK
    return {
        "case_id": case_id,
        "repeat": repeat,
        "family": "F",
        "template": "t1",
        "pack": "furniture",
        "expected": {
            "outcome": "discrepancies_found",
            "types": ["quantity_mismatch"],
            "next_action": TASK,
        },
        "next_action": action,
        "outcome": "discrepancies_found" if right else "manual_review",
        "types": ["quantity_mismatch"] if right else [],
        "error": None,
        "correct": {"next_action": right, "outcome": right, "types": right, "case": right},
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


def test_disagreeing_repeats_are_reported_with_their_sampling_limits() -> None:
    trials = [_trial("A", r, TASK) for r in range(3)]
    trials += [_trial("B", 0, TASK), _trial("B", 1, ESCALATE), _trial("B", 2, TASK)]
    s = summarize_mode(trials)
    assert s["n_cases"] == 2 and s["n_trials"] == 6  # repeats are not extra cases
    assert s["next_action"]["correct"] == 2  # B's majority verdict is right
    assert s["cases"]["B"]["verdicts"] == [TASK, ESCALATE, TASK]
    assert s["cases"]["B"]["flip_rate"] == 0.3333 and s["cases"]["A"]["flip_rate"] == 0
    inst = s["instability"]
    assert inst["cases_with_any_flip"] == 1
    assert inst["mean_flip_rate"] == round((0 + 0.3333) / 2, 4)
    assert (
        inst["cases_with_any_flip_rate"]["n"] == 2 and inst["cases_with_any_flip_rate"]["wilson95"]
    )
    assert inst["repeats_per_case"] == [3]
    assert "below 1/3 cannot be observed" in inst["sampling_limit"]
    assert "do not show that a case is stable" in inst["sampling_limit"]


def test_a_single_repeat_cannot_measure_instability() -> None:
    s = summarize_mode([_trial("A", 0, TASK)])
    assert s["instability"]["repeats_per_case"] == [1]
    assert "cannot be measured" in s["instability"]["sampling_limit"]
