"""Application-level checks on a restored database and artifact store (restore drill).

Run against an isolated restored database only. Checks, per tenant, that every artifact
still held (not deleted by retention) has its bytes in the restored store (directory or
bucket) with the same digest, and that every
citation of the latest investigation of each case still resolves; and that the restored
database still hides rows from application roles without a tenant context (RLS restored).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on

from opsbridge.config import Settings
from opsbridge.domain.records import SourceRef
from opsbridge.evidence.citations import resolve_all
from opsbridge.evidence.storage import ArtifactIntegrityError, ArtifactKeyError, ArtifactStore
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.persistence.models import Artifact, Case, InvestigationVersion


def artifact_problems(
    store: ArtifactStore, tenant_id: uuid.UUID, artifacts: Iterable[Any]
) -> list[str]:
    """Each artifact's bytes must be readable from the store and hash to its recorded digest."""
    bad: list[str] = []
    for a in artifacts:
        try:
            data = store.get(tenant_id, str(a.storage_key))
        except FileNotFoundError:
            bad.append(f"missing {a.id}")
            continue
        except ArtifactIntegrityError:
            bad.append(f"digest mismatch {a.id}")
            continue
        except ArtifactKeyError:
            bad.append(f"key outside tenant {a.id}")
            continue
        if "sha256:" + hashlib.sha256(data).hexdigest() != a.sha256:
            bad.append(f"digest mismatch {a.id}")
    return bad


def verify_restored(settings: Settings, database: str, store: ArtifactStore) -> dict[str, Any]:
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
                bad = artifact_problems(store, tid, arts)
                deleted = s.execute(
                    select(func.count()).select_from(Artifact).where(Artifact.storage_key.is_(None))
                ).scalar_one()
                latest = (
                    select(InvestigationVersion)
                    .ext(distinct_on(InvestigationVersion.case_id))
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
