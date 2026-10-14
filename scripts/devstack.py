#!/usr/bin/env python3
"""Run the OpsBridge application processes locally with health checks.

Infrastructure (PostgreSQL, Keycloak) runs in containers via `make infra-up`.
This script starts the API, worker, simulator, MCP services and UI dev server,
prefixes their logs, waits for each health endpoint, and stops everything on Ctrl-C
or when any process exits.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


@dataclass
class Proc:
    name: str
    cmd: list[str]
    cwd: Path
    health: str | None
    popen: subprocess.Popen[str] | None = None


def processes() -> list[Proc]:
    backend = ROOT / "backend"
    return [
        Proc("simulator", [PY, "-m", "opsbridge.cli", "simulator", "--port", "8090"], backend,
             "http://127.0.0.1:8090/healthz"),
        Proc("api", [PY, "-m", "opsbridge.cli", "api", "--port", "8000"], backend, "http://127.0.0.1:8000/healthz"),
        Proc("records-mcp", [PY, "-m", "opsbridge.cli", "mcp-records", "--port", "8101"], backend,
             "http://127.0.0.1:8101/healthz"),
        Proc("casework-mcp", [PY, "-m", "opsbridge.cli", "mcp-casework", "--port", "8102"], backend,
             "http://127.0.0.1:8102/healthz"),
        Proc("worker", [PY, "-m", "opsbridge.cli", "worker"], backend, None),
        Proc("ui", ["npm", "run", "dev", "--", "--host", "127.0.0.1"], ROOT / "frontend", "http://127.0.0.1:5173/"),
    ]


def available_commands() -> set[str]:
    out = subprocess.run([PY, "-m", "opsbridge.cli", "--help"], capture_output=True, text=True,
                         cwd=ROOT / "backend", timeout=60)
    return {c for c in ("simulator", "api", "mcp-records", "mcp-casework", "worker") if c in out.stdout}


def pump(proc: Proc) -> None:
    assert proc.popen and proc.popen.stdout
    for line in proc.popen.stdout:
        sys.stdout.write(f"[{proc.name:>12}] {line}")
    sys.stdout.flush()


def wait_healthy(proc: Proc, timeout: float = 60.0) -> bool:
    if proc.health is None:
        time.sleep(1.0)
        return proc.popen is not None and proc.popen.poll() is None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.popen is not None and proc.popen.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(proc.health, timeout=2) as r:  # noqa: S310 - fixed local URL
                if r.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-records", action="store_true", help="load synthetic simulator records first")
    parser.add_argument("--only", default="", help="comma-separated subset of processes")
    args = parser.parse_args()

    have = available_commands()
    selected = [p for p in processes() if p.name == "ui" or p.cmd[3] in have]
    if args.only:
        wanted = set(args.only.split(","))
        selected = [p for p in selected if p.name in wanted]
    skipped = sorted({p.name for p in processes()} - {p.name for p in selected})
    if skipped:
        print(f"Not started (not selected or not implemented yet): {', '.join(skipped)}")

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    running: list[Proc] = []

    def stop(*_: object) -> None:
        for p in reversed(running):
            if p.popen and p.popen.poll() is None:
                p.popen.terminate()
        for p in running:
            if p.popen:
                try:
                    p.popen.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.popen.kill()

    signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))
    try:
        if args.seed_records and "simulator" in have:
            subprocess.run([PY, "-m", "opsbridge.cli", "simulator-seed"], cwd=ROOT / "backend", check=False)
        for p in selected:
            p.popen = subprocess.Popen(p.cmd, cwd=p.cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            running.append(p)
            threading.Thread(target=pump, args=(p,), daemon=True).start()
            if not wait_healthy(p):
                print(f"[devstack] {p.name} failed its health check; stopping.")
                stop()
                return 1
            print(f"[devstack] {p.name} healthy" + (f" ({p.health})" if p.health else ""))
        print("[devstack] all selected processes running. UI: http://localhost:5173  (Ctrl-C to stop)")
        while all(p.popen and p.popen.poll() is None for p in running):
            time.sleep(1)
        dead = [p.name for p in running if p.popen and p.popen.poll() is not None]
        print(f"[devstack] process exited: {', '.join(dead)}; stopping the rest.")
        stop()
        return 1
    except KeyboardInterrupt:
        stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
