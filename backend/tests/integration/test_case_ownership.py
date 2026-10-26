"""Case ownership and the work queue's views (review Package 11 / F18: queue ownership and
recovery views).

Before: nothing could make a person a case's owner (the queue's owner column stayed empty),
the queue had no way to show what waits for whom, and operations whose outcome was unknown
were only visible as a count on the settings page and inside each case.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .stack import get, investigate, open_case
from .test_api_cases import call
from .test_approvals_execution import ORG, approve, drain, fault, prepared

pytestmark = pytest.mark.integration
T = f"/api/v1/tenants/{ORG}"


def user_id(st: dict[str, Any], who: str) -> str:
    return str(st["actors"][who].user_id)


def set_owner(
    st: dict[str, Any], who: str, case_id: str, owner: str | None, expected: str | None
) -> httpx.Response:
    return call(
        st["client"], st["actors"][who], "POST", f"{T}/cases/{case_id}/owner",
        json={"owner_id": owner, "expected_owner_id": expected},
    )  # fmt: skip


def queue(st: dict[str, Any], who: str, **params: str) -> dict[str, Any]:
    r = get(st, who, f"{T}/cases?" + "&".join(f"{k}={v}" for k, v in params.items()))
    assert r.status_code == 200, r.text
    return dict(r.json())


def ids(page: dict[str, Any]) -> list[str]:
    return [i["id"] for i in page["items"]]


def test_an_investigator_takes_assigns_and_releases_a_case(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    ivy, max_ = user_id(st, "furn-investigator"), user_id(st, "multi-member")
    case_id = open_case(st, "furn-investigator", ORG, "INV-9001")
    assert get(st, "furn-investigator", f"{T}/cases/{case_id}").json()["owner"] is None
    assert case_id in ids(queue(st, "furn-investigator", view="unassigned"))

    taken = set_owner(st, "furn-investigator", case_id, ivy, None)
    assert taken.status_code == 200 and taken.json()["owner"] == "Ivy Investigator", taken.text
    mine = queue(st, "furn-investigator", view="mine")
    assert ids(mine) == [case_id] and mine["items"][0]["owner"] == "Ivy Investigator"
    assert get(st, "furn-investigator", f"{T}/cases/views").json()["mine"] == 1

    # A claim made on a stale view does not overwrite the current owner.
    stale = set_owner(st, "multi-member", case_id, max_, None)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "owner_changed"
    assert stale.json()["error"]["details"]["current_owner_id"] == ivy
    approver = set_owner(st, "furn-investigator", case_id, user_id(st, "furn-approver"), ivy)
    assert approver.status_code == 409 and approver.json()["error"]["code"] == "not_assignable"
    assert set_owner(st, "furn-approver", case_id, None, ivy).status_code == 403

    given = set_owner(st, "furn-investigator", case_id, max_, ivy)
    assert given.status_code == 200 and given.json()["assignee_id"] == max_
    released = set_owner(st, "multi-member", case_id, None, max_)
    assert released.status_code == 200 and released.json()["owner"] is None
    names = {m["username"] for m in get(st, "furn-investigator", f"{T}/assignees").json()["items"]}
    assert names == {"furn-investigator", "multi-member"}  # active investigators only


def test_a_case_owned_by_someone_who_left_counts_as_unassigned(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    max_ = user_id(st, "multi-member")
    case_id = open_case(st, "furn-investigator", ORG, "INV-9001")
    assert set_owner(st, "multi-member", case_id, max_, None).status_code == 200
    assert case_id not in ids(queue(st, "furn-investigator", view="unassigned"))
    members = get(st, "furn-admin", f"{T}/settings").json()["members"]
    [membership] = [m["id"] for m in members if m["username"] == "multi-member"]
    r = call(
        st["client"], st["actors"]["furn-admin"], "PATCH", f"{T}/members/{membership}",
        json={"status": "revoked", "reason": "left the team"},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    page = queue(st, "furn-investigator", view="unassigned")
    [row] = [i for i in page["items"] if i["id"] == case_id]
    assert row["owner_active"] is False and row["owner"]


def test_views_show_what_waits_for_a_decision_or_a_human_check(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    waiting, _ = prepared(st, "INV-9001")  # a proposal, not decided yet
    stuck, prop = prepared(st, "INV-9003")
    st["store"].set_capabilities(ORG, False, False)  # nothing can prove what happened
    try:
        assert approve(st, stuck, prop).status_code == 200
        fault(st, "timeout_after_commit")
        drain(st)
    finally:
        st["store"].set_capabilities(ORG, True, True)
        httpx.delete(f"{st['base']}/_control/faults")

    approvals = queue(st, "furn-approver", view="awaiting_approval")
    assert ids(approvals) == [waiting] and approvals["items"][0]["awaiting_approval"] is True
    review = queue(st, "furn-approver", view="needs_review")
    assert ids(review) == [stuck] and review["items"][0]["needs_review"] is True
    counts = get(st, "furn-approver", f"{T}/cases/views").json()
    assert (counts["awaiting_approval"], counts["needs_review"]) == (1, 1)
    bad = get(st, "furn-approver", f"{T}/cases?view=everything")
    assert bad.status_code == 422


def test_the_queue_can_be_read_oldest_first_page_by_page(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    made = [open_case(st, "furn-investigator", ORG, f"INV-OLD-{i}") for i in range(3)]
    first = queue(st, "furn-investigator", sort="oldest", limit="2")
    assert ids(first) == made[:2] and first["next_cursor"]
    rest = queue(st, "furn-investigator", sort="oldest", limit="2", cursor=first["next_cursor"])
    assert ids(rest) == made[2:] and rest["next_cursor"] is None
    newest = queue(st, "furn-investigator", limit="3")
    assert ids(newest) == list(reversed(made))


def test_a_closed_case_keeps_its_owner(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    ivy = user_id(st, "furn-investigator")
    case_id = open_case(st, "furn-investigator", ORG, "INV-9002")  # matches policy
    assert set_owner(st, "furn-investigator", case_id, ivy, None).status_code == 200
    investigate(st, "furn-investigator", ORG, case_id)
    r = call(
        st["client"], st["actors"]["furn-investigator"], "POST", f"{T}/cases/{case_id}/resolve",
        json={"note": "Matches"},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    after = set_owner(st, "furn-investigator", case_id, None, ivy)
    assert after.status_code == 409 and after.json()["error"]["code"] == "not_assignable"
    assert case_id not in ids(queue(st, "furn-investigator", view="mine"))  # mine: open only
