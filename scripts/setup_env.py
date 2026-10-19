#!/usr/bin/env python3
"""Create .env from .env.example without overwriting existing secrets.

Every value equal to __GENERATE__ becomes a random URL-safe token. Existing keys in
.env are preserved; keys missing from .env are appended. Secrets are never printed.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v
    return out


def main() -> int:
    example = (ROOT / ".env.example").read_text().splitlines()
    env_path = ROOT / ".env"
    existing = parse(env_path.read_text().splitlines()) if env_path.exists() else {}
    added: list[str] = []
    lines_out: list[str] = []
    if env_path.exists():
        lines_out = env_path.read_text().splitlines()
    for line in example:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            if not env_path.exists():
                lines_out.append(line)
            continue
        key, value = s.split("=", 1)
        if key in existing:
            continue
        if value == "__GENERATE__":
            value = secrets.token_urlsafe(24)
        lines_out.append(f"{key}={value}")
        added.append(key)
    env_path.write_text("\n".join(lines_out) + "\n")
    env_path.chmod(0o600)
    print(f".env ready ({len(added)} key(s) added, {len(existing)} preserved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
