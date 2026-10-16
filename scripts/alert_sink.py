#!/usr/bin/env python3
"""Local stand-in for a pager or ticket system: records Alertmanager webhook notifications.

Used by the observability profile and the alert drill (docs/operations.md). Alertmanager
posts to /<receiver>; GET /notifications returns what arrived. Keeps only the fields the
drill needs, in memory, bounded. Not for staging: there the receivers are real services.
Usage: python3 scripts/alert_sink.py [--host 127.0.0.1] [--port 9099]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 1 << 20
MAX_KEPT = 500
RECEIVERS = {"page", "ticket"}

_lock = threading.Lock()
_kept: deque[dict[str, Any]] = deque(maxlen=MAX_KEPT)


def summarize(receiver: str, body: dict[str, Any]) -> dict[str, Any]:
    alerts = body.get("alerts") or []
    return {
        "received_at": time.time(),
        "receiver": receiver,
        "status": body.get("status"),
        "group_key": body.get("groupKey"),
        "alerts": [
            {
                "status": a.get("status"),
                "labels": {k: str(v) for k, v in (a.get("labels") or {}).items()},
                "starts_at": a.get("startsAt"),
                "ends_at": a.get("endsAt"),
            }
            for a in alerts[:50]
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        receiver = self.path.strip("/")
        if receiver not in RECEIVERS:
            self._send(404, {"error": "unknown receiver"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._send(413, {"error": "body missing, malformed or too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._send(400, {"error": "expected a JSON object"})
            return
        note = summarize(receiver, body)
        with _lock:
            _kept.append(note)
        names = ",".join(
            sorted({a["labels"].get("alertname", "?") for a in note["alerts"]})
        )
        print(f"{receiver}: {note['status']} {names}", flush=True)
        self._send(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/notifications":
            with _lock:
                kept = list(_kept)
            self._send(200, kept)
        elif self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        pass  # one line per notification is printed above


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")  # the container passes 0.0.0.0
    p.add_argument("--port", type=int, default=9099)
    a = p.parse_args()
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"alert sink listening on {a.host}:{a.port}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
