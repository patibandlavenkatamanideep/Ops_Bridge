#!/usr/bin/env python3
"""Fail if tracked (or staged) files contain local secrets or common credential patterns.

Compares every value of length >= 16 from .env whose key looks secret against file
contents, and searches for well-known token formats. Prints file names and key names
only, never values.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_KEY = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE)", re.I)
PATTERNS = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,})"
)


def main() -> int:
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                           cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    secrets: dict[str, str] = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if SECRET_KEY.search(k) and len(v.strip()) >= 16:
                    secrets[k] = v.strip()
    hits: list[str] = []
    for f in files:
        path = ROOT / f
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        data = path.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{f}: value of {k}" for k, v in secrets.items() if v in data]
        hits += [f"{f}: pattern {m.group(0)[:8]}…" for m in PATTERNS.finditer(data)]
    print(f"checked {len(files)} files against {len(secrets)} local secret values")
    for h in hits:
        print("LEAK", h)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
