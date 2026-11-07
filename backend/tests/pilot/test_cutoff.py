"""Only what existed at the cut-off is measured (review N05, protocol opsbridge-pilot/2).

A report at a cut-off must not change for anything done after it: a reviewer decision
recorded later did not exist then (the member has no reference and the report is
incomplete), a process decision or resolution later is not reached, and a manual invoice
started later had not started. The window must be since < until <= as_of, every bound with a
time zone, at every public entry point. (What OpsBridge itself recorded is read as it stood at
the cut-off by pilot/collect.py: tests/integration/test_pilot_report.py.)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from opsbridge.pilot.measures import check_window, report

from .test_measures import CASES, COHORT, MATCHED, QUERY, REFERENCE, at, cohort, facts, ref


def test_the_window_needs_zones_and_its_order() -> None:
    naive = datetime(2026, 11, 2, 10)
    for since, until, as_of in [
        (naive, at(1), at(2)),
        (at(0), naive, at(2)),
        (at(0), at(1), naive),
        (at(1), at(1), at(2)),  # empty
        (at(0), at(2), at(1)),  # the cut-off inside the window
    ]:
        with pytest.raises(ValueError):
            check_window(since, until, as_of)
        with pytest.raises(ValueError):
            report(CASES, REFERENCE, COHORT, since=since, until=until, as_of=as_of)


def test_a_decision_recorded_after_the_cut_off_did_not_exist_then() -> None:
    rows = [ref("opsbridge", "INV-A", 2, reviewed_at=at(5))]
    early = report(
        [facts("CASE-A")], rows, cohort("INV-A"), since=at(-24), until=at(1), as_of=at(3)
    )
    assert not early["complete"]
    assert early["coverage"]["opsbridge"]["references"] == {"after_cut_off": 1}
    assert early["headline"]["n"] == 0 and "cohort lines 2" in early["incomplete_because"][0]
    late = report([facts("CASE-A")], rows, cohort("INV-A"), since=at(-24), until=at(1), as_of=at(6))
    # By then it exists, but it was recorded after intake: not blind, so not in the headline.
    assert late["complete"] and late["headline"]["n"] == 0
    assert late["coverage"]["opsbridge"]["references"] == {"not_blind": 1}
    assert late["accuracy"]["opsbridge"]["not_blind"]["next_action"]["correct"] == 1


def test_a_manual_decision_or_start_after_the_cut_off_is_not_reached() -> None:
    rows = [
        ref("manual", "M-1", 2, reference=MATCHED, process=MATCHED, started_at=at(-1),
            decided_at=at(5), resolved_at=at(6)),
        ref("manual", "M-2", 3, reference=QUERY, started_at=at(4)),  # started after the cut-off
        ref("manual", "M-3", 4, reference=QUERY, started_at=at(-30)),  # before the window
    ]  # fmt: skip
    result = report([], rows, cohort("M-1", "M-2", "M-3"), since=at(-24), until=at(1), as_of=at(3))
    assert result["coverage"]["manual"]["progress"] == {
        "started": 1, "not_started": 1, "outside_window": 1,
    }  # fmt: skip
    manual = result["accuracy"]["manual"]
    assert manual["next_action"]["n"] == 0 and manual["without_process_decision"] == 1
    assert result["time_hours"]["manual"]["to_decision"] == {
        "n": 1, "reached": 0, "open": 1, "median": None, "p90": None,
    }  # fmt: skip
    assert result["time_hours"]["manual"]["not_started"] == 2
