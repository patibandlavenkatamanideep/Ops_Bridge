#!/usr/bin/env python3
"""Wait for a Prometheus alert to reach a state (used in the alert drill, docs/operations.md).

Usage: python3 scripts/alert_drill.py ALERT [label=value ...] --state firing|resolved [--timeout 600]
Prints the elapsed seconds; exits 1 on timeout. Read-only: queries /api/v1/alerts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def active(url: str, name: str, labels: dict[str, str]) -> list[dict[str, object]]:
    with urllib.request.urlopen(f"{url}/api/v1/alerts", timeout=5) as r:  # noqa: S310 - local URL
        alerts = json.load(r)["data"]["alerts"]
    return [
        a
        for a in alerts
        if a["labels"].get("alertname") == name and all(a["labels"].get(k) == v for k, v in labels.items())
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("alert")
    p.add_argument("labels", nargs="*")
    p.add_argument("--state", choices=["firing", "resolved"], required=True)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--prometheus", default="http://127.0.0.1:9090")
    a = p.parse_args()
    labels = dict(x.split("=", 1) for x in a.labels)
    start = time.monotonic()
    while time.monotonic() - start < a.timeout:
        found = active(a.prometheus, a.alert, labels)
        firing = any(x["state"] == "firing" for x in found)
        if (a.state == "firing" and firing) or (a.state == "resolved" and not found):
            print(f"{a.alert} {labels} {a.state} after {time.monotonic() - start:.0f}s")
            return 0
        time.sleep(5)
    print(f"timeout: {a.alert} {labels} not {a.state} within {a.timeout:.0f}s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
