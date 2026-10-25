"""Retention never deletes bytes another artifact still uses (review finding F08).

Artifacts of one tenant share the bytes of identical content (one storage key). The sweep
deleted those bytes as soon as one row reached its cutoff, so a newer upload of the same file
lost its content, and it deleted them before the database committed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, text

from opsbridge.evidence.storage import LocalArtifactStore
from opsbridge.persistence.db import Database, DbContext
from opsbridge.workflow import retention

from .stack import get
from .test_api_cases import call
from .test_approvals_execution import _tenant_of
from .test_hardening import upload

pytestmark = pytest.mark.integration
ORG = "demo-furniture"
T = f"/api/v1/tenants/{ORG}"
NOTE = b"Delivery note DN-77: 20 chairs, signed by the warehouse."


class SimulatedCrash(BaseException):
    pass


def new_case(st: dict[str, Any], title: str) -> str:
    r = call(
        st["client"], st["actors"]["furn-investigator"], "POST", f"{T}/cases", json={"title": title}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def attach(st: dict[str, Any], case_id: str) -> str:
    r = upload(
        st["client"],
        st["actors"]["furn-investigator"],
        case_id,
        "attachment",
        "dn.txt",
        NOTE,
        "text/plain",
    )
    assert r.status_code == 201, r.text
    return str(r.json()["version_id"])


def original(st: dict[str, Any], case_id: str, version_id: str) -> Any:
    return get(
        st, "furn-investigator", f"{T}/cases/{case_id}/documents/versions/{version_id}/original"
    )


def age_oldest_copy(worker_db: Database, tid: uuid.UUID) -> None:
    """Make the first upload of NOTE 31 days old; the other copy stays new."""
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        s.execute(
            text(
                "UPDATE artifacts SET created_at = now() - interval '31 days' WHERE id = ("
                "SELECT id FROM artifacts WHERE kind = 'raw_document' AND size_bytes = :n "
                "ORDER BY created_at LIMIT 1)"
            ),
            {"n": len(NOTE)},
        )


def set_retention(st: dict[str, Any]) -> None:
    r = call(
        st["client"], st["actors"]["furn-admin"], "PUT", f"{T}/settings",
        json={"retention": {"raw_documents_days": 30}},
    )  # fmt: skip
    assert r.status_code == 200, r.text


def on_disk(st: dict[str, Any]) -> list[Any]:
    import hashlib

    return list(st["settings"].artifact_dir.rglob(hashlib.sha256(NOTE).hexdigest()))


def test_bytes_another_artifact_still_uses_are_kept(
    stack, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    a, b = new_case(st, "Old delivery note"), new_case(st, "Same note, attached again")
    va, vb = attach(st, a), attach(st, b)
    tid = _tenant_of(migrator_engine, ORG)
    age_oldest_copy(worker_db, tid)
    set_retention(st)
    report = retention.sweep(worker_db, LocalArtifactStore(st["settings"].artifact_dir), tid)
    assert report.artifacts_deleted == 1 and report.blobs_kept == 1 and report.blobs_deleted == 0
    assert original(st, a, va).status_code == 404  # the old copy is gone from OpsBridge
    newer = original(st, b, vb)
    assert newer.status_code == 200 and newer.content == NOTE  # its bytes were not deleted


def test_bytes_go_only_after_the_rows_commit_and_a_crash_is_finished_later(
    stack, worker_db: Database, migrator_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    a = new_case(st, "Delivery note")
    va = attach(st, a)
    tid = _tenant_of(migrator_engine, ORG)
    age_oldest_copy(worker_db, tid)
    set_retention(st)
    store = LocalArtifactStore(st["settings"].artifact_dir)

    def dies(*_a: object, **_k: object) -> None:
        raise SimulatedCrash("worker died after the rows were committed")

    monkeypatch.setattr(retention, "purge_pending", dies)
    with pytest.raises(SimulatedCrash):
        retention.sweep(worker_db, store, tid)
    monkeypatch.undo()
    assert original(st, a, va).status_code == 404  # rows committed first
    assert on_disk(st)  # bytes not yet deleted: queued, not lost track of
    # The same file arrives again before anything purges the queue: it must survive.
    b = new_case(st, "Note re-sent")
    vb = attach(st, b)
    assert retention.purge_pending(worker_db, store, tid) == (0, 1, 0)  # deleted, kept, failed
    assert original(st, b, vb).content == NOTE
    with worker_db.session(DbContext(tenant_id=tid)) as s:
        assert (
            s.execute(text("SELECT count(*) FROM blob_deletions WHERE done_at IS NULL")).scalar()
            == 0
        )


def test_a_storage_failure_leaves_the_deletion_queued(
    stack, worker_db: Database, migrator_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    attach(st, new_case(st, "Delivery note"))
    tid = _tenant_of(migrator_engine, ORG)
    age_oldest_copy(worker_db, tid)
    set_retention(st)
    store = LocalArtifactStore(st["settings"].artifact_dir)

    def unavailable(*_a: object, **_k: object) -> bool:
        raise OSError("object store unavailable")

    monkeypatch.setattr(store, "delete", unavailable)
    report = retention.sweep(worker_db, store, tid)
    assert (report.artifacts_deleted, report.blob_failures) == (1, 1) and on_disk(st)
    monkeypatch.undo()
    assert retention.purge_pending(worker_db, store, tid) == (1, 0, 0)
    assert not on_disk(st)
