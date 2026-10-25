#!/usr/bin/env python3
"""Run every gate in a fixed list and report each one honestly.

Every gate appears in every report: PASSED, FAILED, or SKIPPED with the missing prerequisite.
Tests that a test runner skipped inside a passing gate are counted and shown. By default the
exit status is non-zero only if a gate failed (a convenience check). With --strict (`make
verify-strict`, the release gate) a skipped gate or a skipped test also fails: a release is
qualified only by gates that actually ran. Separately authorized live checks (marker `live`)
are not part of either.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "UV_PROJECT_ENVIRONMENT": ".venv.nosync",
       "PYTHONPYCACHEPREFIX": str(ROOT / ".pycache.nosync")}
PY = ["uv", "--directory", "backend", "run", "--frozen"]


def up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


STRICT = False
SKIPPED_TESTS = re.compile(r"(\d+) skipped")  # pytest and Playwright summaries


def run(name: str, cmd: list[str], cwd: Path = ROOT, extra_env: dict[str, str] | None = None) -> tuple[str, str]:
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=cwd, env={**ENV, **(extra_env or {})}, capture_output=True, text=True)
    took = f"{time.monotonic() - start:.1f}s"
    output = (proc.stdout + proc.stderr).strip()
    tail = output.splitlines()[-1:] or [""]
    status = "PASSED" if proc.returncode == 0 else "FAILED"
    # pytest puts the count on its last line, Playwright on a line of its own above it.
    skipped = sum(int(n) for line in output.splitlines()[-6:] for n in SKIPPED_TESTS.findall(line))
    if status == "PASSED" and skipped and STRICT:
        status = "FAILED"  # a release gate runs its tests; a skip hides a missing prerequisite
    print(f"{status:8} {name:28} {took:>7}  {tail[0][:110]}")
    if proc.returncode != 0:
        print("\n".join("         | " + line for line in output.splitlines()[-25:]))
    elif skipped:
        reasons = [ln.strip() for ln in output.splitlines() if ln.startswith("SKIPPED")][:5]
        print(f"         | {skipped} test(s) skipped" + "".join(f"\n         | {r[:150]}" for r in reasons))
    return name, status


def skip(name: str, reason: str) -> tuple[str, str]:
    print(f"{'SKIPPED':8} {name:28}          {reason}")
    return name, "SKIPPED"


def main() -> int:
    global STRICT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on skipped gates or tests")
    STRICT = parser.parse_args().strict
    results: list[tuple[str, str]] = []
    results.append(run("backend lint", [*PY, "ruff", "check", "--no-cache", "src", "tests"]))
    results.append(run("backend format", [*PY, "ruff", "format", "--no-cache", "--check", "src", "tests"]))
    results.append(run("backend typecheck", [*PY, "mypy", "src"]))
    results.append(run("backend unit tests", [*PY, "pytest", "-q", "-rs", "-m", "not integration and not keycloak and not live"]))
    results.append(run("frontend lint", ["npm", "run", "lint"], ROOT / "frontend"))
    results.append(run("frontend typecheck", ["npm", "run", "typecheck"], ROOT / "frontend"))
    results.append(run("frontend unit tests", ["npm", "test"], ROOT / "frontend"))
    results.append(run("evaluation canary (pinned baseline)", [*PY, "python", "-m", "opsbridge.cli", "eval-canary"]))
    # Read-only: re-hash every sealed benchmark receipt (replays run via make eval-offline).
    results.append(run("evaluation receipts (eval-verify)", [*PY, "python", "-m", "opsbridge.cli", "eval-verify"]))

    db = up(5433)
    no_db = "PostgreSQL :5433 not running (make infra-up)"
    results.append(run("migration drift (alembic check)", [*PY, "alembic", "check"])
                   if db else skip("migration drift (alembic check)", no_db))
    results.append(run("alert rules and routing (promtool, amtool)", ["make", "-s", "alerts-test"], ROOT)
                   if db else skip("alert rules and routing (promtool, amtool)", no_db))
    # Offline replay of the recorded benchmark runs; seals nothing (make eval-offline does).
    results.append(run("benchmark replay reproduces recordings",
                       [*PY, "python", "-m", "opsbridge.cli", "eval-replay-check"])
                   if db else skip("benchmark replay reproduces recordings", no_db))
    if db and up(8081):
        results.append(run("integration tests", [*PY, "pytest", "-q", "-rs", "-m", "integration or keycloak"],
                           extra_env={"OPSBRIDGE_REQUIRE_INTEGRATION": "1"}))
    else:
        results.append(skip("integration tests", "PostgreSQL :5433 / Keycloak :8081 not running (make infra-up)"))

    if up(5173) and up(8000):
        results.append(run("browser tests", ["npx", "playwright", "test"], ROOT / "frontend"))
    else:
        results.append(skip("browser tests", "UI :5173 / API :8000 not running (make dev)"))

    results.append(run("frontend build", ["npm", "run", "build"], ROOT / "frontend"))

    failed = [n for n, s in results if s == "FAILED"]
    skipped = [n for n, s in results if s == "SKIPPED"]
    print(f"\n{len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped"
          + (f" (skipped: {', '.join(skipped)})" if skipped else ""))
    if STRICT and skipped:
        print("STRICT: a skipped gate does not qualify a release; start its prerequisites and rerun.")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
