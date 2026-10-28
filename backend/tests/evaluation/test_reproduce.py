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


# --- replay comparison 2: behaviour, not only the next action (production review A13) -----


def _trial(case: str, repeat: int, **changes: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "case_id": case,
        "repeat": repeat,
        "next_action": "propose_action:create_follow_up_task",
        "error": None,
        "outcome": "discrepancies_found",
        "types": ["quantity_mismatch"],
        "finding_codes": ["invoiced_exceeds_received"],
        "explanation_origin": "model:mock",
        "citations": 3,
        "citations_valid": 3,
        "model_calls": 2,
        "model_calls_rejected": 0,
        "run_status": "completed",
        "response_mode": "mocked",
        "proposal_validation": {"accepted": True, "model_call_id": "1d3c", "problems": []},
        "tool_calls": [
            {
                "seq": 1,
                "tool": "get_invoice",
                "version": "records/1",
                "input_sha256": "a",
                "response_sha256": "b",
                "outcome": "ok",
                "origin": "live",
            }
        ],
        "findings_sha256": "sha256:f",
        "citations_sha256": "sha256:c",
        "latency_ms": 120,
    }
    t.update(changes)
    return t


def test_the_same_action_with_different_findings_does_not_reproduce() -> None:
    from opsbridge.evaluation.reproduce import trial_differences

    recorded = {"mock": [_trial("a", 0), _trial("a", 1)]}
    for change in (
        {"finding_codes": ["price_above_po_beyond_tolerance"]},
        {"explanation_origin": "template"},
        {"types": []},
        {"outcome": "manual_review"},
        {"citations_valid": 2},
        {"findings_sha256": "sha256:other values"},
        {"citations_sha256": "sha256:other sources"},
        {"tool_calls": []},
        {"proposal_validation": {"accepted": False, "model_call_id": "x", "problems": ["p"]}},
    ):
        replayed = {"mock": [_trial("a", 0, **change), _trial("a", 1)]}
        diffs, _ = trial_differences(recorded, replayed)
        assert len(diffs) == 1 and diffs[0].startswith("mock a#0:"), (change, diffs)


def test_ids_timings_and_replay_origins_are_not_behaviour() -> None:
    from opsbridge.evaluation.reproduce import trial_differences

    recorded = {"mock": [_trial("a", 0)]}
    replayed_tool = dict(_trial("a", 0)["tool_calls"][0], origin="recorded")
    replayed = {
        "mock": [
            _trial(
                "a",
                0,
                latency_ms=9999,
                explanation_origin="model:replay",
                proposal_validation={"accepted": True, "model_call_id": "other", "problems": []},
                tool_calls=[replayed_tool],
            )
        ]
    }
    assert trial_differences(recorded, replayed) == ([], [])


def test_missing_or_extra_repeats_and_fields_not_recorded_are_reported() -> None:
    from opsbridge.evaluation.reproduce import trial_differences

    old = {
        k: v for k, v in _trial("a", 0).items() if k not in ("findings_sha256", "citations_sha256")
    }
    diffs, not_recorded = trial_differences(
        {"mock": [old, _trial("b", 0)]}, {"mock": [_trial("a", 0), _trial("a", 1)]}
    )
    assert diffs == ["mock a#1: not in the recording", "mock b#0: not replayed"]
    assert not_recorded == ["citations_sha256", "findings_sha256"]  # compared where recorded
