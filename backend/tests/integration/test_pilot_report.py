"""The pilot report from OpsBridge's own records (protocol opsbridge-pilot/2, docs/pilot-plan.md).

Cases are worked through the API as a pilot would work them: a follow-up approved as
drafted, a supplier query approved after its body was edited, a matched invoice resolved,
and a follow-up rejected. The cohort, frozen before intake, enrols them with a manual-arm
invoice; the reviewer's reference file gives blind decisions, one recorded after intake that
disagrees, and an exclusion. Each OpsBridge row names its invoice, since a blind decision is
recorded before the case and its number exist. The report reads only the pilot tenant's cases
in the window, in one read-only, repeatable-read transaction, writes nothing, and names cases
by number, never by invoice.

Review N05: the report is of the cut-off. Anything done after it (an approval, a second
investigation, a correction) changes nothing, a change committed while the report reads is
in none of its queries, and a cut-off later than the database's clock is refused.
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from opsbridge import cli
from opsbridge.persistence.db import Database, DbContext
from opsbridge.pilot import collect as collect_module
from opsbridge.pilot.cohort import COLUMNS as COHORT_COLUMNS
from opsbridge.pilot.collect import collect
from opsbridge.pilot.reference import COLUMNS

from .conftest import TEST_DB
from .stack import get, investigate, open_case
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


def db_now(engine: Engine) -> datetime:
    """The database's clock: the one every recorded time, and so the cut-off, is on."""
    with engine.begin() as conn:
        return conn.execute(text("SELECT clock_timestamp()")).scalar_one()  # type: ignore[no-any-return]


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


def write_cohort(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as f:
        out = csv.DictWriter(f, fieldnames=COHORT_COLUMNS, restval="")
        out.writeheader()
        out.writerows(rows)
    return path


def audit_rows(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one())


BLIND_FOLLOW_UP = {
    "outcome": "discrepancies_found",
    "types": "quantity_mismatch",
    "next_action": "propose_action:create_follow_up_task",
}


def test_the_report_measures_worked_cases_against_the_reference(
    stack: dict[str, Any], migrator_engine: Engine, worker_db: Database, tmp_path: Path
) -> None:
    st = stack
    before = db_now(migrator_engine) - timedelta(minutes=5)

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
    cohort = write_cohort(
        tmp_path / "cohort.csv",
        [
            {"arm": "opsbridge", "invoice": n}
            for n in ("INV-9001", "INV-9009", "INV-9002", "INV-9003")
        ]
        + [{"arm": "manual", "case": "M-1"}],
    )
    reference = write_reference(
        tmp_path / "reference.csv",
        [
            # recorded before the case reached OpsBridge: blind, and it agrees
            {"arm": "opsbridge", "invoice": "INV-9001", "reviewed_at": before.isoformat(), **BLIND_FOLLOW_UP},
            {  # recorded afterwards, and the reviewer would have escalated
                "arm": "opsbridge",
                "invoice": "inv-9009",  # as typed; intake normalizes it the same way
                "reviewed_at": db_now(migrator_engine).isoformat(),
                "outcome": "discrepancies_found",
                "types": "price_mismatch",
                "next_action": "escalate:assign_case",
            },
            {
                "arm": "opsbridge", "invoice": "INV-9002", "reviewed_at": before.isoformat(),
                "outcome": "matched", "next_action": "no_discrepancy:record_review_note",
            },
            {"arm": "opsbridge", "invoice": "INV-9003", "excluded": "duplicate of a paid invoice"},
            {
                "arm": "manual",
                "case": "M-1",
                "reviewed_at": before.isoformat(),
                "outcome": "matched",
                "next_action": "no_discrepancy:record_review_note",
                "process_outcome": "matched",
                "process_next_action": "no_discrepancy:record_review_note",
                "started_at": before.isoformat(),
                "decided_at": (before + timedelta(minutes=3)).isoformat(),
            },
        ],
    )  # fmt: skip
    out = tmp_path / "report.json"
    audit_before = audit_rows(migrator_engine)
    tid = tenant_id(migrator_engine, "demo-furniture")
    cut = db_now(migrator_engine)
    assert (
        cli.main([
            "pilot-report", "--tenant-id", str(tid), "--since", before.isoformat(),
            "--until", cut.isoformat(), "--as-of", cut.isoformat(), "--cohort", str(cohort),
            "--reference", str(reference), "--out", str(out), "--database", TEST_DB,
        ])
        == 0
    )  # fmt: skip
    assert audit_rows(migrator_engine) == audit_before  # the report wrote nothing
    report = json.loads(out.read_text())
    assert "INV-9" not in out.read_text()

    assert report["protocol"] == "opsbridge-pilot/2" and report["complete"]
    database = report["provenance"]["database"]
    assert database["isolation"] == "repeatable read" and database["read_only"] is True
    assert len(report["provenance"]["reference_sha256"]) == 64 and report["cohort"]["members"] == 5
    coverage = report["coverage"]["opsbridge"]
    assert coverage["enrolled"] == 4 and coverage["investigated"] == 3  # demo-it's case absent
    assert coverage["references"] == {"blind": 2, "not_blind": 1}
    assert coverage["excluded"] == [
        {"case": numbers["rejected"], "cohort_line": 5, "reason": "duplicate of a paid invoice"}
    ]

    assert report["extraction"]["evidence_readiness"] == {"complete": 3}

    ops = report["accuracy"]["opsbridge"]
    assert ops["blind"]["case"]["correct"] == 2 and ops["blind"]["case"]["n"] == 2
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
        "n": 1, "reached": 1, "open": 0, "median": 0.05, "p90": 0.05,
    }  # fmt: skip

    # The window is the intake window: nothing before it is read.
    later = cut + timedelta(microseconds=1)
    with pytest.raises(ValueError, match="later than the database's clock"):
        collect(
            worker_db,
            tid,
            since=cut,
            until=later + timedelta(days=1),
            as_of=later + timedelta(days=1),
        )
    assert (
        collect(worker_db, tid, since=before - timedelta(days=1), until=before, as_of=cut).cases
        == []
    )


def _invoice_quantity(st: dict[str, Any], case_id: str) -> dict[str, Any]:
    docs = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents").json()["items"]
    inv = next(d for d in docs if d["kind"] == "invoice")
    url = f"{T}/cases/{case_id}/documents/versions/{inv['versions'][-1]['version_id']}"
    fields = get(st, "furn-investigator", url).json()["fields"]
    return dict(next(f for f in fields if f["field_path"] == "invoice_line.quantity"))


def test_nothing_done_after_the_cut_off_changes_a_report_of_it(
    stack: dict[str, Any], migrator_engine: Engine, worker_db: Database
) -> None:
    """Before: a report as of the cut-off counted an approval made after it, as drafted."""
    st = stack
    since = db_now(migrator_engine)
    case_id, proposal = prepared(st, "INV-9001")
    tid = tenant_id(migrator_engine, "demo-furniture")
    cut = db_now(migrator_engine)

    def facts() -> Any:
        [facts] = [
            f for f in collect(worker_db, tid, since=since, until=cut, as_of=cut).cases
            if f.case_number == case_number(st, case_id)
        ]  # fmt: skip
        return facts

    then = facts()
    assert then.first_approved_at is None and [c.status for c in then.chains] == [
        "awaiting_approval"
    ]
    # After the cut-off: approved, a field corrected, investigated again.
    assert approve(st, case_id, proposal).status_code == 200
    qty = _invoice_quantity(st, case_id)
    r = call(
        st["client"], st["actors"]["furn-investigator"], "POST",
        f"{T}/cases/{case_id}/fields/{qty['field_id']}/corrections",
        json={"new_value": "18", "reason": "Receiving counted 18"},
    )  # fmt: skip
    assert r.status_code in (200, 201), r.text
    investigate(st, "furn-investigator", ORG, case_id)
    assert facts() == then  # the same facts, read again after all of it


def test_a_change_committed_while_the_report_reads_is_in_none_of_its_queries(
    stack: dict[str, Any],
    migrator_engine: Engine,
    worker_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the default isolation each query saw the latest commit, so a report could count
    a case and then an approval made after it had been listed."""
    st = stack
    since = db_now(migrator_engine)
    case_id, proposal = prepared(st, "INV-9001")
    tid = tenant_id(migrator_engine, "demo-furniture")
    cut = db_now(migrator_engine) + timedelta(seconds=30)  # the approval falls before it
    real = collect_module._facts
    approved: list[int] = []

    def approve_first(*args: Any) -> Any:
        if not approved:  # between listing the cases and reading their facts
            approved.append(approve(st, case_id, proposal).status_code)
        return real(*args)

    monkeypatch.setattr(collect_module, "_facts", approve_first)
    with pytest.raises(ValueError, match="later than the database's clock"):
        collect(worker_db, tid, since=since, until=cut, as_of=cut)
    cut = db_now(migrator_engine)
    collected = collect(worker_db, tid, since=since, until=cut, as_of=cut + timedelta(seconds=0))
    assert approved == [200]
    [facts] = collected.cases
    assert facts.first_approved_at is None  # committed after the snapshot began: not seen
    assert collected.snapshot.isolation == "repeatable read" and collected.snapshot.read_only


def test_the_report_cannot_write_and_leaves_its_connection_as_it_was(
    worker_db: Database,
) -> None:
    # The worker role may write to these tables; the report's transaction may not.
    tid = uuid.uuid4()
    with pytest.raises(DBAPIError, match="read-only transaction"):
        with worker_db.snapshot(DbContext(tenant_id=tid)) as s:
            s.execute(
                text(
                    "INSERT INTO audit_events (tenant_id, actor_kind, actor_id, action, "
                    "resource_type, resource_id, outcome, details) VALUES (:t, 'system', "
                    "'pilot-report', 'probe', 'case', 'x', 'ok', '{}')"
                ),
                {"t": tid},
            )
    # Pooled connections return with their usual isolation and access.
    with worker_db.session() as s:
        assert s.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        assert s.execute(text("SHOW transaction_read_only")).scalar_one() == "off"
