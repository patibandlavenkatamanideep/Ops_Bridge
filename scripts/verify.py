#!/usr/bin/env python3
"""Run every locally available gate and report each one honestly.

A gate whose prerequisite is missing is reported as SKIPPED with the reason; it is
never reported as passed. Exit status is non-zero if any executed gate failed.
"""

from __future__ import annotations

import os
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


def run(name: str, cmd: list[str], cwd: Path = ROOT, extra_env: dict[str, str] | None = None) -> tuple[str, str]:
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=cwd, env={**ENV, **(extra_env or {})}, capture_output=True, text=True)
    took = f"{time.monotonic() - start:.1f}s"
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    status = "PASSED" if proc.returncode == 0 else "FAILED"
    print(f"{status:8} {name:28} {took:>7}  {tail[0][:110]}")
    if proc.returncode != 0:
        print("\n".join("         | " + line for line in (proc.stdout + proc.stderr).strip().splitlines()[-25:]))
    return name, status


def main() -> int:
    results: list[tuple[str, str]] = []
    results.append(run("backend lint", [*PY, "ruff", "check", "src", "tests"]))
    results.append(run("backend format", [*PY, "ruff", "format", "--check", "src", "tests"]))
    results.append(run("backend typecheck", [*PY, "mypy", "src"]))
    results.append(run("backend unit tests", [*PY, "pytest", "-q", "-m", "not integration and not keycloak"]))
    results.append(run("frontend lint", ["npm", "run", "lint"], ROOT / "frontend"))
    results.append(run("frontend typecheck", ["npm", "run", "typecheck"], ROOT / "frontend"))
    results.append(run("frontend unit tests", ["npm", "test"], ROOT / "frontend"))

    if up(5433) and up(8081):
        results.append(run("integration tests", [*PY, "pytest", "-q", "-m", "integration or keycloak"],
                           extra_env={"OPSBRIDGE_REQUIRE_INTEGRATION": "1"}))
    else:
        print(f"{'SKIPPED':8} {'integration tests':28}          PostgreSQL :5433 / Keycloak :8081 not running (make infra-up)")
        results.append(("integration tests", "SKIPPED"))

    if up(5173) and up(8000):
        results.append(run("browser tests", ["npx", "playwright", "test"], ROOT / "frontend"))
    else:
        print(f"{'SKIPPED':8} {'browser tests':28}          UI :5173 / API :8000 not running (make dev)")
        results.append(("browser tests", "SKIPPED"))

    results.append(run("frontend build", ["npm", "run", "build"], ROOT / "frontend"))

    failed = [n for n, s in results if s == "FAILED"]
    skipped = [n for n, s in results if s == "SKIPPED"]
    print(f"\n{len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped"
          + (f" (skipped: {', '.join(skipped)})" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
