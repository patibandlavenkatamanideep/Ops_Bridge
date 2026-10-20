#!/usr/bin/env python3
"""Run every panel query of the OpsBridge dashboard through Grafana (make observability-up).

Read-only, as Grafana's anonymous Viewer: checks that Grafana is healthy, that the provisioned
dashboard exists, and that each panel query runs against Prometheus without an error. A query
with no data is reported, not failed (some series appear only after their first event).
Usage: python3 scripts/dashboard_check.py [--grafana http://127.0.0.1:3000]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

UID = "opsbridge-overview"


def get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - local URL
        return json.load(r)


def query(base: str, ds: dict[str, Any], expr: str) -> tuple[str | None, int]:
    body = json.dumps(
        {
            "queries": [
                {"refId": "A", "datasource": ds, "expr": expr, "intervalMs": 15000, "maxDataPoints": 200}
            ],
            "from": "now-1h",
            "to": "now",
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/api/ds/query", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - local URL
            result = json.load(r)["results"]["A"]
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read()[:200]!r}", 0
    if result.get("error"):
        return str(result["error"]), 0
    points = sum(len(f["data"]["values"][-1]) for f in result.get("frames", []) if f["data"]["values"])
    return None, points


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--grafana", default="http://127.0.0.1:3000")
    a = p.parse_args()
    health = get(f"{a.grafana}/api/health")
    print(f"grafana {health.get('version')}: database {health.get('database')}")
    dash = get(f"{a.grafana}/api/dashboards/uid/{UID}")["dashboard"]
    errors = empty = 0
    for panel in dash["panels"]:
        if panel["type"] == "row":
            continue
        for t in panel["targets"]:
            err, points = query(a.grafana, t["datasource"], t["expr"])
            if err:
                errors += 1
                print(f"  ERROR    {panel['title']}: {err}")
            elif points == 0:
                empty += 1
                print(f"  no data  {panel['title']}")
            else:
                print(f"  ok       {panel['title']} ({points} points)")
    print(f"{dash['title']}: {errors} error(s), {empty} panel query(ies) without data yet")
    return 1 if errors or health.get("database") != "ok" else 0


if __name__ == "__main__":
    sys.exit(main())
