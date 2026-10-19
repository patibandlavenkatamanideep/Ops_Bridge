#!/usr/bin/env python3
"""Wait for a Prometheus alert to reach a state (used in the alert drill, docs/operations.md).

Usage: python3 scripts/alert_drill.py ALERT [label=value ...] --state firing|resolved
           [--timeout 600] [--sink http://127.0.0.1:9099 [--receiver page|ticket]]
Prints the elapsed seconds; exits 1 on timeout. With --sink it also waits until the
notification for that state reached the local alert sink (scripts/alert_sink.py) through
Alertmanager, and fails if it went to a different receiver than --receiver.
Read-only: queries /api/v1/alerts and the sink's /notifications.
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


def delivered(sink: str, name: str, labels: dict[str, str], status: str, since: float) -> str | None:
    """Receiver of the first notification after `since` carrying this alert in `status`."""
    with urllib.request.urlopen(f"{sink}/notifications", timeout=5) as r:  # noqa: S310 - local URL
        notes = json.load(r)
    for n in notes:
        if n["received_at"] < since:
            continue
        for x in n["alerts"]:
            ls = x["labels"]
            if ls.get("alertname") == name and x["status"] == status and all(ls.get(k) == v for k, v in labels.items()):
                return str(n["receiver"])
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("alert")
    p.add_argument("labels", nargs="*")
    p.add_argument("--state", choices=["firing", "resolved"], required=True)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--prometheus", default="http://127.0.0.1:9090")
    p.add_argument("--sink", help="alert sink URL; also wait for the notification")
    p.add_argument("--receiver", choices=["page", "ticket"], help="receiver the notification must reach")
    a = p.parse_args()
    labels = dict(x.split("=", 1) for x in a.labels)
    since, start = time.time(), time.monotonic()
    reached = False
    while time.monotonic() - start < a.timeout:
        if not reached:
            found = active(a.prometheus, a.alert, labels)
            firing = any(x["state"] == "firing" for x in found)
            if (a.state == "firing" and firing) or (a.state == "resolved" and not found):
                reached = True
                print(f"{a.alert} {labels} {a.state} in Prometheus after {time.monotonic() - start:.0f}s")
                if not a.sink:
                    return 0
        if a.sink:
            receiver = delivered(a.sink, a.alert, labels, a.state, since)
            if receiver:
                print(f"{a.alert} {a.state} notification delivered to '{receiver}' after {time.monotonic() - start:.0f}s")
                if a.receiver and receiver != a.receiver:
                    print(f"wrong receiver: expected '{a.receiver}'")
                    return 1
                return 0
        time.sleep(5)
    print(f"timeout: {a.alert} {labels} not {a.state}{' and delivered' if a.sink else ''} within {a.timeout:.0f}s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
