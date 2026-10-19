#!/usr/bin/env python3
"""Local backup and restore drill (synthetic demo data only).

  backup        pg_dump (custom format) of `opsbridge` as the container superuser (RLS is
                FORCEd, so only a superuser dump is complete), a tar of the artifact store,
                and a manifest with digests, row counts and the migration revision.
                Written to var/backups/<UTC timestamp>/ (not committed).
  restore-drill Restore a backup into the isolated database `opsbridge_restore_drill`,
                compare row counts and revision, check RLS is still forced, extract the
                artifacts to a scratch directory, run application-level checks
                (`opsbridge verify-restore`), record timings in restore-drill.json, then drop
                the drill database. Never touches `opsbridge`.

Usage: python3 scripts/backup_restore.py backup
       python3 scripts/backup_restore.py restore-drill [BACKUP_DIR] [--keep]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "var" / "backups"
DRILL_DB = "opsbridge_restore_drill"
ROLES = "opsbridge_migrator, opsbridge_app, opsbridge_worker, opsbridge_mcp"
COMPOSE = 'eval "$(scripts/container-env.sh)" && docker compose --env-file .env -f infra/compose/docker-compose.yml'


def compose(args: str, *, stdin: Path | None = None, stdout: Path | None = None) -> str:
    cmd = f"{COMPOSE} {args}"
    with (stdin.open("rb") if stdin else open("/dev/null", "rb")) as fin:  # noqa: SIM115
        if stdout:
            with stdout.open("wb") as fout:
                p = subprocess.run(["bash", "-c", cmd], cwd=ROOT, stdin=fin, stdout=fout, stderr=subprocess.PIPE, check=False)  # noqa: S603, S607
            out = ""
        else:
            p = subprocess.run(["bash", "-c", cmd], cwd=ROOT, stdin=fin, capture_output=True, check=False)  # noqa: S603, S607
            out = p.stdout.decode()
    if p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}): {args}\n{p.stderr.decode()[-2000:]}")
    return out


def psql(db: str, sql: str) -> str:
    return compose(f"exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d {db} -tA -c {json.dumps(sql)}").strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def row_counts(db: str) -> dict[str, int]:
    tables = psql(db, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").split()
    sql = " UNION ALL ".join(f"SELECT '{t}', count(*) FROM public.{t}" for t in tables)
    return {name: int(n) for name, n in (line.split("|") for line in psql(db, sql).splitlines())}


def backup() -> Path:
    out = BACKUPS / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True)
    t0 = time.monotonic()
    dump = out / "opsbridge.dump"
    compose("exec -T postgres pg_dump -U postgres -Fc opsbridge", stdout=dump)
    t_dump = time.monotonic() - t0
    artifacts = ROOT / "var" / "artifacts"
    tar_path = out / "artifacts.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        if artifacts.exists():
            tar.add(artifacts, arcname="artifacts")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_database": "opsbridge",
        "data": "synthetic demo data only",
        "alembic_revision": psql("opsbridge", "SELECT version_num FROM alembic_version"),
        "row_counts": row_counts("opsbridge"),
        "files": {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size} for p in (dump, tar_path)},
        "seconds": {"pg_dump": round(t_dump, 2), "total": round(time.monotonic() - t0, 2)},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"backup → {out.relative_to(ROOT)} ({manifest['files']['opsbridge.dump']['bytes']} B dump, "
          f"{sum(manifest['row_counts'].values())} rows, revision {manifest['alembic_revision']})")
    return out


def restore_drill(src: Path, keep: bool) -> int:
    manifest = json.loads((src / "manifest.json").read_text())
    report: dict[str, object] = {"backup": src.name, "started_at": datetime.now(UTC).isoformat(timespec="seconds")}
    for name, meta in manifest["files"].items():
        if sha256(src / name) != meta["sha256"]:
            sys.exit(f"backup file {name} does not match its manifest digest; not restoring")
    t0 = time.monotonic()
    psql("postgres", f"DROP DATABASE IF EXISTS {DRILL_DB}")
    psql("postgres", f"CREATE DATABASE {DRILL_DB} OWNER opsbridge_migrator")
    compose(f"exec -T postgres pg_restore -U postgres -d {DRILL_DB} --exit-on-error", stdin=src / "opsbridge.dump")
    psql(DRILL_DB, f"REVOKE CONNECT ON DATABASE {DRILL_DB} FROM PUBLIC; GRANT CONNECT ON DATABASE {DRILL_DB} TO {ROLES}")
    t_restore = time.monotonic() - t0
    counts = row_counts(DRILL_DB)
    unforced = psql(
        DRILL_DB,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version' "
        "AND NOT (c.relrowsecurity AND c.relforcerowsecurity)",
    )
    scratch = ROOT / "var" / "restore-drill"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    with tarfile.open(src / "artifacts.tar.gz") as tar:
        tar.extractall(scratch, filter="data")
    app = subprocess.run(  # noqa: S603
        ["uv", "--directory", "backend", "run", "--frozen", "python", "-m", "opsbridge.cli", "verify-restore",  # noqa: S607
         "--database", DRILL_DB, "--artifact-dir", str(scratch / "artifacts")],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    try:
        app_report = json.loads(app.stdout[app.stdout.index("{"):]) if "{" in app.stdout else {"error": app.stderr[-1000:]}
    except ValueError:
        app_report = {"error": app.stdout[-1000:] + app.stderr[-1000:]}
    report.update(
        {
            "seconds": {"restore": round(t_restore, 2), "total": round(time.monotonic() - t0, 2)},
            "alembic_revision": {"backup": manifest["alembic_revision"], "restored": psql(DRILL_DB, "SELECT version_num FROM alembic_version")},
            "row_counts_equal": counts == manifest["row_counts"],
            "rows_restored": sum(counts.values()),
            "tables_without_forced_rls": int(unforced),
            "application_checks": app_report,
        }
    )
    ok = (
        report["row_counts_equal"]
        and int(unforced) == 0
        and manifest["alembic_revision"] == report["alembic_revision"]["restored"]  # type: ignore[index]
        and app.returncode == 0
    )
    report["ok"] = ok
    if not keep:
        psql("postgres", f"DROP DATABASE {DRILL_DB}")
        shutil.rmtree(scratch, ignore_errors=True)
        report["cleanup"] = "drill database dropped; scratch artifacts removed"
    (src / "restore-drill.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "application_checks"}, indent=1, default=str))
    print("application checks:", json.dumps(app_report.get("tenants", app_report), default=str))
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup")
    r = sub.add_parser("restore-drill")
    r.add_argument("backup_dir", nargs="?")
    r.add_argument("--keep", action="store_true", help="keep the drill database and scratch files")
    a = p.parse_args()
    if a.cmd == "backup":
        backup()
        return 0
    src = Path(a.backup_dir) if a.backup_dir else max(BACKUPS.iterdir(), key=lambda d: d.name)
    return restore_drill(src, a.keep)


if __name__ == "__main__":
    sys.exit(main())
