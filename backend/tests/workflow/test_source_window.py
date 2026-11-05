"""How long an investigation's purchasing data may be acted on (review N03).

An investigation records when it read the purchasing system and until when its result may be
acted on: the read time plus `source_max_age_hours`. For an export the read time is the time
the export asserts, so an export dated ahead of the clock gave a window that long ahead too.
Now the data's age is the earlier of the source's time and this host's, a window is never
later than the investigation's own record allows, and an investigation recorded before that
(no observed time) is held to when it was stored. Clocks here are injected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opsbridge.services.freshness import source_window_problem
from opsbridge.workflow.investigation import source_times

STORED = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


def recorded(read: datetime, *, observed: datetime | None = None) -> dict[str, str]:
    state = {"source_read_at": read.isoformat(), "source_valid_until": (read + DAY).isoformat()}
    if observed is not None:
        state["source_observed_at"] = observed.isoformat()
    return state


def test_a_result_may_be_acted_on_until_its_window_closes_and_not_after() -> None:
    state = recorded(STORED - timedelta(hours=1), observed=STORED - timedelta(hours=1))
    closes = STORED - timedelta(hours=1) + DAY
    assert source_window_problem(state, STORED, now=STORED) is None
    assert source_window_problem(state, STORED, now=closes) is None  # the boundary itself
    late = source_window_problem(state, STORED, now=closes + timedelta(microseconds=1))
    assert late is not None and "longer ago than the limit" in late


def test_without_a_recorded_read_time_a_result_is_not_acted_on() -> None:
    problem = source_window_problem({}, STORED, now=STORED)
    assert problem is not None and "does not record when it read" in problem


def test_a_read_time_ahead_of_when_the_result_was_stored_gives_no_longer_window() -> None:
    """Recorded before this change: an export dated a year ahead, so its window closed a year
    and a day after the investigation. It is held to a day after the investigation."""
    state = recorded(STORED + timedelta(days=365))
    assert source_window_problem(state, STORED, now=STORED + DAY) is None
    late = source_window_problem(state, STORED, now=STORED + DAY + timedelta(seconds=1))
    assert late is not None


def test_the_datas_age_is_the_earlier_of_the_sources_time_and_this_hosts() -> None:
    observed = STORED
    # A source a minute ahead: the data is taken as read now, not a minute from now.
    ahead = source_times(asserted=observed + timedelta(minutes=1), observed=observed)
    assert ahead["read_at"] == observed.isoformat()
    assert ahead["asserted_at"] == (observed + timedelta(minutes=1)).isoformat()
    # An export taken two days ago is two days old however recently it is read.
    old = source_times(asserted=observed - 2 * DAY, observed=observed)
    assert old["read_at"] == (observed - 2 * DAY).isoformat()
    assert old["observed_at"] == observed.isoformat()
    # A live source asserts nothing: its data is as old as the read.
    assert source_times(asserted=None, observed=observed) == {
        "read_at": observed.isoformat(),
        "observed_at": observed.isoformat(),
    }
