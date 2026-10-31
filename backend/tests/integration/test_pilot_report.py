"""The pilot report from OpsBridge's own records (protocol opsbridge-pilot/1, docs/pilot-plan.md).

Cases are worked through the API as a pilot would work them: a follow-up approved as
drafted, a supplier query approved after its body was edited, a matched invoice resolved,
and a follow-up rejected. The reviewer's reference file gives one blind decision, one
recorded after intake that disagrees, and a manual-arm case. The report reads only the
pilot tenant's cases in the window, in a read-only transaction, and writes nothing.
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from opsbridge import cli
from opsbridge.persistence.db import Database
from opsbridge.pilot.collect import collect, read_only_session
from opsbridge.pilot.reference import COLUMNS

from .conftest import TEST_DB
from .stack import investigate, open_case
from .test_api_cases import call
from .test_approvals_execution import ORG, T, approve, prepared

pytestmark = pytest.mark.integration


def tenant_id(engine: Engine, slug: str) -> uuid.UUID:
    with engine.begin() as conn:
        return uuid.UUID(
            str(
                conn.execute(
                    text("SELECT id FROM tenants WHERE slug = :s"), {"s": slug}
                ).scalar_one()
            )
        )


def case_number(st: dict[str, Any], case_id: str) -> str:
    r = call(st["client"], st["actors"]["furn-investigator"], "GET", f"{T}/cases/{case_id}")
    assert r.status_code == 200, r.text
    return str(r.json()["case_number"])


def write_reference(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as f:
        out = csv.DictWriter(f, fieldnames=COLUMNS, restval="")
        out.writeheader()
        out.writerows(rows)
    return path


def audit_rows(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one())


def test_the_report_measures_worked_cases_against_the_reference(
    stack: dict[str, Any], migrator_engine: Engine, worker_db: Database, tmp_path: Path
) -> None:
    st = stack
    before = datetime.now(UTC) - timedelta(minutes=5)

    drafted_id, drafted = prepared(st, "INV-9001")  # quantity follow-up, approved as drafted
    assert approve(st, drafted_id, drafted).status_code == 200

    edited_id, query = prepared(st, "INV-9009")  # supplier price query, edited, then approved
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "PUT",
        f"{T}/cases/{edited_id}/proposals/{query['id']}",
        json={
            "expected_payload_digest": query["payload_digest"],
            "body": "Please confirm the price.",
        },
    )
    assert r.status_code == 201, r.text
    assert approve(st, edited_id, r.json()).status_code == 200

    matched_id = open_case(st, "furn-investigator", ORG, "INV-9002")  # matched, then resolved
    investigate(st, "furn-investigator", ORG, matched_id)
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{matched_id}/resolve",
        json={"note": "Matches policy."},
    )
    assert r.status_code == 200, r.text

    rejected_id, rejected = prepared(st, "INV-9003")
    r = call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{T}/cases/{rejected_id}/proposals/{rejected['id']}/decision",
        json={
            "decision": "reject",
            "version": rejected["version"],
            "payload_digest": rejected["payload_digest"],
            "reason": "Receiving already posted it.",
        },
    )
    assert r.status_code == 200, r.text

    # Another tenant's case in the same window must not appear.
    open_case(st, "it-requester", "demo-it", "INV-9101")

    numbers = {k: case_number(st, v) for k, v in [("drafted", drafted_id), ("edited", edited_id),
               ("matched", matched_id), ("rejected", rejected_id)]}  # fmt: skip
    reference = write_reference(
        tmp_path / "reference.csv",
        [
            {  # recorded before the case reached OpsBridge: blind, and it agrees
                "arm": "opsbridge",
                "case": numbers["drafted"],
                "reviewed_at": before.isoformat(),
                "outcome": "discrepancies_found",
                "types": "quantity_mismatch",
                "next_action": "propose_action:create_follow_up_task",
            },
            {  # recorded afterwards, and the reviewer would have escalated
                "arm": "opsbridge",
                "case": numbers["edited"],
                "reviewed_at": datetime.now(UTC).isoformat(),
                "outcome": "discrepancies_found",
                "types": "price_mismatch",
                "next_action": "escalate:assign_case",
            },
            {
                "arm": "opsbridge",
                "case": numbers["rejected"],
                "excluded": "duplicate of a paid invoice",
            },
            {
                "arm": "manual",
                "case": "M-1",
                "reviewed_at": before.isoformat(),
                "outcome": "matched",
                "next_action": "no_discrepancy:record_review_note",
                "process_outcome": "matched",
                "process_next_action": "no_discrepancy:record_review_note",
                "started_at": before.isoformat(),
                "decided_at": (before + timedelta(hours=3)).isoformat(),
            },
        ],
    )
    out = tmp_path / "report.json"
    audit_before = audit_rows(migrator_engine)
    tid = tenant_id(migrator_engine, "demo-furniture")
    after = datetime.now(UTC) + timedelta(minutes=5)
    assert (
        cli.main([
            "pilot-report", "--tenant-id", str(tid), "--since", before.isoformat(),
            "--until", after.isoformat(), "--as-of", (after + timedelta(hours=4)).isoformat(),
            "--reference", str(reference), "--out", str(out), "--database", TEST_DB,
        ])
        == 0
    )  # fmt: skip
    assert audit_rows(migrator_engine) == audit_before  # the report wrote nothing
    report = json.loads(out.read_text())

    assert report["protocol"] == "opsbridge-pilot/1"
    scope = report["scope"]
    assert scope["opsbridge_cases"] == 3 and scope["manual_cases"] == 1  # demo-it's case absent
    assert scope["unreferenced"] == [numbers["matched"]] and scope["unknown_references"] == []

    assert report["extraction"]["evidence_readiness"] == {"complete": 3}

    ops = report["accuracy"]["opsbridge"]
    assert ops["blind"]["case"]["correct"] == 1 and ops["blind"]["case"]["n"] == 1
    assert ops["not_blind"]["next_action"]["correct"] == 0
    assert [(d["case"], d["got"]["next_action"]) for d in ops["disagreements"]] == [
        (numbers["edited"], "propose_action:create_supplier_message_draft")
    ]
    assert report["accuracy"]["manual"]["case"]["correct"] == 1

    approval = report["approval"]  # the rejected case was excluded by the reviewer
    assert (approval["chains"], approval["approved_as_drafted"], approval["approved_changed"]) == (
        2,
        1,
        1,
    )
    assert approval["changed_fields"] == {"body": 1}

    times = report["time_hours"]
    assert times["opsbridge"]["to_decision"]["reached"] == 3  # two approvals and a resolution
    assert times["opsbridge"]["to_resolution"]["reached"] == 1
    assert times["manual"]["to_decision"] == {
        "n": 1,
        "reached": 1,
        "open": 0,
        "median": 3.0,
        "p90": 3.0,
    }

    # The window is the intake window: nothing before it is read.
    empty = collect(worker_db, tid, since=after, until=after + timedelta(days=1))
    assert empty == []


def test_the_report_cannot_write(worker_db: Database) -> None:
    # The worker role may write to these tables; the report's transaction may not.
    tid = uuid.uuid4()
    with pytest.raises(DBAPIError, match="read-only transaction"):
        with read_only_session(worker_db, tid) as s:
            s.execute(
                text(
                    "INSERT INTO audit_events (tenant_id, actor_kind, actor_id, action, "
                    "resource_type, resource_id, outcome, details) VALUES (:t, 'system', "
                    "'pilot-report', 'probe', 'case', 'x', 'ok', '{}')"
                ),
                {"t": tid},
            )
