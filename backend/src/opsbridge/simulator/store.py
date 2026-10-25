"""SQLite persistence for the purchasing-system simulator (an external system stand-in)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  org TEXT NOT NULL, collection TEXT NOT NULL, key TEXT NOT NULL,
  po_number TEXT, content_type TEXT NOT NULL, body BLOB NOT NULL, sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL, deleted_at TEXT, PRIMARY KEY (org, collection, key));
CREATE INDEX IF NOT EXISTS ix_records_po ON records (org, collection, po_number);
CREATE TABLE IF NOT EXISTS tasks (
  org TEXT NOT NULL, id TEXT NOT NULL, idempotency_key TEXT, marker TEXT,
  title TEXT NOT NULL, body TEXT NOT NULL, assignee_group TEXT, status TEXT NOT NULL,
  version INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (org, id));
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_idem ON tasks (org, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_marker ON tasks (org, marker);
CREATE TABLE IF NOT EXISTS faults (
  id TEXT PRIMARY KEY, org TEXT NOT NULL, route TEXT NOT NULL, mode TEXT NOT NULL,
  remaining INTEGER NOT NULL, delay_ms INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capabilities (
  org TEXT PRIMARY KEY, idempotency INTEGER NOT NULL, search INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS request_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, org TEXT, route TEXT, outcome TEXT, at TEXT);
"""


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Fault:
    id: str
    org: str
    route: str
    mode: str
    remaining: int
    delay_ms: int


class SimStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._page_limits: dict[str, int] = {}
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(records)")}
            if "deleted_at" not in columns:  # a store created before deletions existed
                self._conn.execute("ALTER TABLE records ADD COLUMN deleted_at TEXT")

    # --- records ------------------------------------------------------------------------

    def put_record(
        self,
        org: str,
        collection: str,
        key: str,
        body: bytes,
        content_type: str,
        po_number: str | None,
    ) -> str:
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            self._conn.execute(
                "INSERT INTO records (org, collection, key, po_number, content_type, body, sha256, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (org, collection, key) DO UPDATE SET "
                "po_number=excluded.po_number, content_type=excluded.content_type, body=excluded.body, "
                "sha256=excluded.sha256, updated_at=excluded.updated_at, deleted_at=NULL",
                (org, collection, key, po_number, content_type, body, digest, _now()),
            )
        return digest

    def delete_record(self, org: str, collection: str, key: str) -> bool:
        """Delete a record, keeping a tombstone that listings report (as a voided or reversed
        document would be in a purchasing system). Putting the record again restores it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE records SET deleted_at=?, updated_at=? "
                "WHERE org=? AND collection=? AND key=? AND deleted_at IS NULL",
                (_now(), _now(), org, collection, key),
            )
        return cur.rowcount == 1

    def get_record(self, org: str, collection: str, key: str) -> tuple[bytes, str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body, content_type, sha256 FROM records "
                "WHERE org=? AND collection=? AND key=? AND deleted_at IS NULL",
                (org, collection, key),
            ).fetchone()
        return (bytes(row[0]), row[1], row[2]) if row else None

    def list_records(
        self,
        org: str,
        collection: str,
        po_number: str | None,
        after: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of records (tombstones included) in key order, after the cursor `after`.
        Returns the page and the cursor for the next one (None on the last page)."""
        size = min(limit or DEFAULT_PAGE_SIZE, self._page_limits.get(org, MAX_PAGE_SIZE))
        sql = (
            "SELECT key, content_type, sha256, updated_at, deleted_at FROM records "
            "WHERE org=? AND collection=?"
        )
        args: list[Any] = [org, collection]
        if po_number is not None:
            sql += " AND po_number=?"
            args.append(po_number)
        if after is not None:
            sql += " AND key > ?"
            args.append(after)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY key LIMIT ?", [*args, size + 1]).fetchall()
        page = [
            {
                "key": r[0],
                "content_type": r[1],
                "sha256": r[2],
                "updated_at": r[3],
                "deleted": r[4] is not None,
            }
            for r in rows[:size]
        ]
        return page, (page[-1]["key"] if len(rows) > size else None)

    def set_page_limit(self, org: str, size: int | None) -> None:
        """The largest page the source serves for this org (None: the default)."""
        with self._lock:
            if size is None:
                self._page_limits.pop(org, None)
            else:
                self._page_limits[org] = size

    def clear_org(self, org: str) -> None:
        with self._lock:
            for table in ("records", "tasks", "faults", "capabilities"):
                self._conn.execute(f"DELETE FROM {table} WHERE org=?", (org,))  # noqa: S608 - fixed names
            self._page_limits.pop(org, None)

    # --- tasks --------------------------------------------------------------------------

    def create_task(
        self,
        org: str,
        title: str,
        body: str,
        assignee_group: str | None,
        marker: str | None,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Create (or return the existing task for an idempotency key). Returns (task, created)."""
        with self._lock:
            if idempotency_key is not None:
                row = self._conn.execute(
                    "SELECT id FROM tasks WHERE org=? AND idempotency_key=?", (org, idempotency_key)
                ).fetchone()
                if row:
                    task = self.get_task(org, row[0])
                    assert task is not None
                    return task, False
            tid = f"T-{uuid.uuid4().hex[:10].upper()}"
            self._conn.execute(
                "INSERT INTO tasks (org, id, idempotency_key, marker, title, body, assignee_group, status, "
                "version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (org, tid, idempotency_key, marker, title, body, assignee_group, "open", 1, _now()),
            )
            task = self.get_task(org, tid)
            assert task is not None
            return task, True

    def get_task(self, org: str, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, idempotency_key, marker, title, body, assignee_group, status, version, created_at "
                "FROM tasks WHERE org=? AND id=?",
                (org, task_id),
            ).fetchone()
        if not row:
            return None
        keys = (
            "id",
            "idempotency_key",
            "marker",
            "title",
            "body",
            "assignee_group",
            "status",
            "version",
            "created_at",
        )
        return dict(zip(keys, row, strict=True))

    def find_tasks(self, org: str, marker: str) -> list[dict[str, Any]]:
        with self._lock:
            ids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT id FROM tasks WHERE org=? AND marker=? ORDER BY created_at",
                    (org, marker),
                ).fetchall()
            ]
        return [t for t in (self.get_task(org, i) for i in ids) if t is not None]

    def count_tasks(self, org: str) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT count(*) FROM tasks WHERE org=?", (org,)).fetchone()[0]
            )

    # --- faults, capabilities, log ----------------------------------------------------

    def add_fault(self, org: str, route: str, mode: str, count: int, delay_ms: int) -> str:
        fid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO faults (id, org, route, mode, remaining, delay_ms, created_at) VALUES (?,?,?,?,?,?,?)",
                (fid, org, route, mode, count, delay_ms, _now()),
            )
        return fid

    def take_fault(self, org: str, route: str) -> Fault | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, org, route, mode, remaining, delay_ms FROM faults WHERE org=? AND route=? "
                "AND remaining > 0 ORDER BY created_at LIMIT 1",
                (org, route),
            ).fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE faults SET remaining = remaining - 1 WHERE id=?", (row[0],))
        return Fault(*row)

    def clear_faults(self, org: str | None = None) -> None:
        with self._lock:
            if org is None:
                self._conn.execute("DELETE FROM faults")
            else:
                self._conn.execute("DELETE FROM faults WHERE org=?", (org,))

    def set_capabilities(self, org: str, idempotency: bool, search: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO capabilities (org, idempotency, search) VALUES (?,?,?) ON CONFLICT (org) "
                "DO UPDATE SET idempotency=excluded.idempotency, search=excluded.search",
                (org, int(idempotency), int(search)),
            )

    def capabilities(self, org: str) -> dict[str, bool]:
        with self._lock:
            row = self._conn.execute(
                "SELECT idempotency, search FROM capabilities WHERE org=?", (org,)
            ).fetchone()
        return (
            {"idempotency": bool(row[0]), "search": bool(row[1])}
            if row
            else {"idempotency": True, "search": True}
        )

    def log(self, org: str, route: str, outcome: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO request_log (org, route, outcome, at) VALUES (?,?,?,?)",
                (org, route, outcome, _now()),
            )

    def stats(self, org: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT route, outcome, count(*) FROM request_log WHERE org=? GROUP BY route, outcome",
                (org,),
            ).fetchall()
        return {"tasks": self.count_tasks(org), "requests": [list(r) for r in rows]}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=False).encode("utf-8")
