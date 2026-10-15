"""Application-level checks on a restored database and artifact store (restore drill).

Run against an isolated restored database only. Checks, per tenant, that every artifact
still held (not deleted by retention) has its bytes and the same digest, and that every
citation of the latest investigation of each case still resolves; and that the restored
database still hides rows from application roles without a tenant context (RLS restored).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from opsbridge.config import Settings
from opsbridge.domain.records import SourceRef
from opsbridge.evidence.citations import resolve_all
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.persistence.models import Artifact, Case, InvestigationVersion


def verify_restored(settings: Settings, database: str, artifact_dir: Path) -> dict[str, Any]:
    migrator = make_engine("migrator", settings, database=database)
    app = make_engine("app", settings, database=database)
    worker = Database(make_engine("worker", settings, database=database))
    report: dict[str, Any] = {"database": database, "tenants": {}}
    try:
        with migrator.connect() as conn:
            tenants = [
                (r[0], r[1])
                for r in conn.execute(text("SELECT id, slug FROM tenants ORDER BY slug"))
            ]
        with app.connect() as conn:
            report["rows_visible_without_tenant_context"] = {
                t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()  # noqa: S608 - fixed names
                for t in ("cases", "source_documents", "investigation_versions", "operations")
            }
        for tid, slug in tenants:
            with worker.session(DbContext(tenant_id=tid)) as s:
                arts = list(
                    s.execute(select(Artifact).where(Artifact.storage_key.is_not(None))).scalars()
                )
                bad: list[str] = []
                for a in arts:
                    path = artifact_dir / str(a.storage_key)
                    if not path.is_file():
                        bad.append(f"missing {a.id}")
                    elif "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != a.sha256:
                        bad.append(f"digest mismatch {a.id}")
                deleted = s.execute(
                    select(func.count()).select_from(Artifact).where(Artifact.storage_key.is_(None))
                ).scalar_one()
                latest = (
                    select(InvestigationVersion)
                    .distinct(InvestigationVersion.case_id)
                    .order_by(InvestigationVersion.case_id, InvestigationVersion.version.desc())
                )
                total = valid = 0
                for iv in s.execute(latest).scalars():
                    refs = [SourceRef(**e) for f in iv.findings for e in f.get("evidence", [])]
                    resolved = resolve_all(s, refs)
                    total += len(resolved)
                    valid += sum(1 for r in resolved if r.status == "valid")
                cases = s.execute(select(func.count()).select_from(Case)).scalar_one()
            report["tenants"][slug] = {
                "cases": cases,
                "artifacts_checked": len(arts),
                "artifacts_deleted_by_retention": deleted,
                "artifact_problems": bad[:20],
                "citations": total,
                "citations_valid": valid,
            }
    finally:
        migrator.dispose()
        app.dispose()
        worker.engine.dispose()
    report["ok"] = all(
        v == 0 for v in report["rows_visible_without_tenant_context"].values()
    ) and all(
        not t["artifact_problems"] and t["citations"] == t["citations_valid"]
        for t in report["tenants"].values()
    )
    return report
