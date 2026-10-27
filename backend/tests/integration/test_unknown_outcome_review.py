"""A person's review of an unknown outcome completes the operation the way the executor
would (production review A05: consistent state for machine completion and human review).

Before: the review set the operation's status and nothing else. A follow-up a person found
in the tracker left the case awaiting review instead of moving it to follow-up pending, and
the operation's outbox event stayed undelivered, so the backlog never drained. The
confirmation stays labelled as a person's finding, not as proof from the provider.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from opsbridge.persistence.db import Database, DbContext

from .stack import get
from .test_api_cases import call
from .test_approvals_execution import (
    T,
    approve,
    drain,
    fault,
    operations,
    prepared,
    remote_tasks,
    sim,
    use_connector,
)

pytestmark = pytest.mark.integration


def _unknown(st: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    use_connector(st, lambda: sim(st, idempotency_keys=False, lookup="none"), timeout_seconds=1.0)
    case_id, prop = prepared(st, "INV-9001")
    approve(st, case_id, prop)
    fault(st, "timeout_after_commit")
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "outcome_unknown"
    return case_id, op


def _review(st: dict[str, Any], case_id: str, op: dict[str, Any], **body: Any) -> Any:
    return call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{T}/cases/{case_id}/operations/{op['id']}/review",
        json={"note": "Checked the tracker", **body},
    )


def _undelivered(worker_db: Database, tenant: str, op_id: str) -> int:
    with worker_db.session(DbContext(tenant_id=tenant)) as s:  # type: ignore[arg-type]
        return int(
            s.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE aggregate_id = :id "
                    "AND delivered_at IS NULL"
                ),
                {"id": op_id},
            ).scalar_one()
        )


def test_a_follow_up_a_person_found_completes_like_a_confirmed_one(
    stack, worker_db: Database
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, op = _unknown(st)
    ref = remote_tasks(st, op)[0]["id"]
    r = _review(st, case_id, op, finding="exists", remote_ref=ref)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    [done] = operations(st, case_id)
    assert done["confirmation"]["method"] == "human_review"  # a person's finding, labelled
    case = get(st, "furn-investigator", f"{T}/cases/{case_id}").json()
    assert case["status"] == "follow_up_pending"  # the follow-up exists; the exception stays open
    tenant = str(case["tenant_id"]) if "tenant_id" in case else _tenant_of(worker_db)
    assert _undelivered(worker_db, tenant, op["id"]) == 0


def test_a_follow_up_a_person_found_absent_leaves_the_case_awaiting_review(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, op = _unknown(st)
    r = _review(st, case_id, op, finding="absent")
    assert r.status_code == 200 and r.json()["status"] == "failed"
    [done] = operations(st, case_id)
    assert done["failure_category"] == "verified_absent_by_human_review"
    case = get(st, "furn-investigator", f"{T}/cases/{case_id}").json()
    assert case["status"] == "awaiting_review"


def _tenant_of(worker_db: Database) -> str:
    with worker_db.session(DbContext()) as s:
        return str(
            s.execute(
                text("SELECT tenant_id FROM jobs WHERE kind = 'dispatch_operation' LIMIT 1")
            ).scalar_one()
        )
