#!/usr/bin/env python3
"""Check local prerequisites. Prints presence/versions only, never secret values."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ENV = [
    "POSTGRES_SUPERUSER_PASSWORD", "OPSBRIDGE_DB_MIGRATOR_PASSWORD", "OPSBRIDGE_DB_APP_PASSWORD",
    "OPSBRIDGE_DB_WORKER_PASSWORD", "OPSBRIDGE_DB_MCP_PASSWORD", "KEYCLOAK_DB_PASSWORD",
    "KEYCLOAK_ADMIN_PASSWORD", "OPSBRIDGE_OIDC_CLIENT_SECRET", "OPSBRIDGE_WORKER_CLIENT_SECRET",
    "OPSBRIDGE_SESSION_SECRET", "OPSBRIDGE_DEMO_USER_PASSWORD",
]


def version(cmd: list[str]) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "present (version unknown)"


def port(p: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.5):
            return True
    except OSError:
        return False


def cloud_synced(path: Path) -> bool:
    """True when the checkout lives in a macOS iCloud-synced Desktop/Documents folder."""
    home = Path.home()
    return any(path.is_relative_to(home / d) for d in ("Desktop", "Documents")) and (
        home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Desktop"
    ).exists()


def main() -> int:
    problems: list[str] = []
    print("Tools")
    for name, cmd, required in [
        ("uv", ["uv", "--version"], True),
        ("node", ["node", "--version"], True),
        ("npm", ["npm", "--version"], True),
        ("docker CLI", ["docker", "--version"], True),
        ("make", ["make", "--version"], False),
        ("ollama (optional, local inference)", ["ollama", "--version"], False),
        ("tesseract (optional, OCR)", ["tesseract", "--version"], False),
    ]:
        v = version(cmd)
        print(f"  {'ok ' if v else '-- '} {name}: {v or 'not found'}")
        if required and not v:
            problems.append(f"{name} is required")
    engine = subprocess.run(["bash", str(ROOT / "scripts" / "container-env.sh")], capture_output=True, text=True)
    engine_ok = engine.returncode == 0 and "false" not in engine.stdout
    kind = "docker context" if engine_ok and not engine.stdout.strip() else ("podman socket" if engine_ok else "unreachable")
    print(f"  {'ok ' if engine_ok else '-- '} container engine: {kind}")
    if not engine_ok:
        problems.append("no reachable Docker/Podman engine")

    print("Workspace")
    if cloud_synced(ROOT):
        print("  -- checkout is inside an iCloud-synced folder; files may be evicted and reads may hang")
        problems.append("move the checkout outside ~/Desktop and ~/Documents (see docs/adr/0009)")
    else:
        print(f"  ok  checkout location: {ROOT}")

    print("Configuration")
    env_path = ROOT / ".env"
    if not env_path.exists():
        # Expected on a fresh checkout: `make setup` creates it with generated local secrets.
        print("  next .env not created yet — `make setup` creates it (secrets generated, never printed)")
    else:
        keys = {line.split("=", 1)[0]: line.split("=", 1)[1].strip()
                for line in env_path.read_text().splitlines() if "=" in line and not line.startswith("#")}
        missing = [k for k in REQUIRED_ENV if not keys.get(k) or keys.get(k) == "__GENERATE__"]
        print(f"  {'ok ' if not missing else '-- '} required secrets set: {len(REQUIRED_ENV) - len(missing)}/{len(REQUIRED_ENV)}"
              + (f" (missing: {', '.join(missing)})" if missing else ""))
        if missing:
            problems.append("required .env values missing")
        for k in ("ANTHROPIC_API_KEY", "OPSBRIDGE_GITHUB_TOKEN"):
            print(f"  info {k}: {'set' if keys.get(k) else 'not set'} (optional; live checks stay blocked without it)")

    print("Services")
    for name, p in [("PostgreSQL :5433", 5433), ("Keycloak :8081", 8081), ("API :8000", 8000),
                    ("Simulator :8090", 8090), ("Records MCP :8101", 8101), ("Casework MCP :8102", 8102),
                    ("UI :5173", 5173)]:
        print(f"  {'up ' if port(p) else 'down'} {name}")

    if problems:
        print("\nProblems: " + "; ".join(problems))
        return 1
    print("\nPrerequisites satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
