#!/usr/bin/env python3
"""Add realm users that a running local Keycloak does not have yet (development only).

Keycloak imports infra/identity/realm-opsbridge.json only when the realm does not exist, so
users added to the file later never reach an existing local Keycloak. This adds the missing
ones through Keycloak's partial import with ifResourceExists=SKIP: existing users, their
passwords and every other realm setting stay as they are. Reads KEYCLOAK_ADMIN_PASSWORD and
the `${...}` placeholders in the realm file from the environment or `.env`; prints no secret.
Usage: python3 scripts/identity_sync.py [--keycloak http://127.0.0.1:8081]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REALM_FILE = ROOT / "infra" / "identity" / "realm-opsbridge.json"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip("'\"")
    values.update(os.environ)
    return values


def substitute(obj: Any, values: dict[str, str]) -> Any:
    if isinstance(obj, str):

        def repl(m: re.Match[str]) -> str:
            if m.group(1) not in values:
                raise SystemExit(f"{m.group(1)} is not set (run make setup)")
            return values[m.group(1)]

        return re.sub(r"\$\{([A-Z0-9_]+)\}", repl, obj)
    if isinstance(obj, list):
        return [substitute(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, values) for k, v in obj.items()}
    return obj


def post(url: str, data: bytes, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - local URL, checked
        return json.load(r)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--keycloak", default="http://127.0.0.1:8081")
    a = p.parse_args()
    if urllib.parse.urlparse(a.keycloak).hostname not in LOCAL_HOSTS:
        print("refusing: this adds development users and only talks to a local Keycloak")
        return 2
    values = env_values()
    if "KEYCLOAK_ADMIN_PASSWORD" not in values:
        print("KEYCLOAK_ADMIN_PASSWORD is not set (run make setup)")
        return 2
    realm = json.loads(REALM_FILE.read_text(encoding="utf-8"))
    token = post(
        f"{a.keycloak}/realms/master/protocol/openid-connect/token",
        urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "admin",
                "password": values["KEYCLOAK_ADMIN_PASSWORD"],
            }
        ).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )["access_token"]
    result = post(
        f"{a.keycloak}/admin/realms/{realm['realm']}/partialImport",
        json.dumps(
            {"ifResourceExists": "SKIP", "users": substitute(realm["users"], values)}
        ).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    added = sorted(r["resourceName"] for r in result.get("results", []) if r["action"] == "ADDED")
    print(
        f"realm {realm['realm']}: {result.get('added', 0)} user(s) added"
        f"{' (' + ', '.join(added) + ')' if added else ''}, {result.get('skipped', 0)} already present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
