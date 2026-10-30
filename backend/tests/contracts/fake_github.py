"""Independent fake of the documented GitHub Issues REST subset (contract-test oracle).

Written from docs.github.com (rest/issues/issues, API version 2026-03-10), not from the
connector. It enforces the documented headers and returns documented status codes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass
class FakeGitHub:
    repo: str = "opsbridge-sandbox/followups"
    token: str = "test-token"
    issues: list[dict[str, Any]] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)  # applied to the next create, FIFO
    hide_recent_seconds: float = 0.0  # simulate a listing that lags behind writes
    requests: list[dict[str, Any]] = field(default_factory=list)
    # `timeout_after` replies wait for this, set by the test when it is done (before the
    # server stops). A timer would race the client's deadline on another clock: on macOS
    # uvloop's clock counts time the host slept and the client's does not.
    release_held_replies: threading.Event = field(default_factory=threading.Event)

    def app(self) -> FastAPI:
        app = FastAPI()
        owner, name = self.repo.split("/")

        def check(request: Request) -> JSONResponse | None:
            self.requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "headers": dict(request.headers),
                }
            )
            if request.headers.get("authorization") != f"Bearer {self.token}":
                return JSONResponse({"message": "Bad credentials"}, status_code=401)
            if request.headers.get("x-github-api-version") != "2026-03-10":
                return JSONResponse({"message": "Unsupported API version"}, status_code=400)
            return None

        @app.post(f"/repos/{owner}/{name}/issues")
        async def create(request: Request) -> Any:
            if (bad := check(request)) is not None:
                return bad
            body = await request.json()
            fault = self.faults.pop(0) if self.faults else None
            if fault == "422":
                return JSONResponse({"message": "Validation Failed"}, status_code=422)
            if fault == "410":
                return JSONResponse(
                    {"message": "Issues are disabled for this repo"}, status_code=410
                )
            if fault == "rate_limited":
                return JSONResponse(
                    {"message": "API rate limit exceeded"},
                    status_code=403,
                    headers={"x-ratelimit-remaining": "0", "retry-after": "1"},
                )
            if fault == "503_before":
                return JSONResponse({"message": "Service unavailable"}, status_code=503)
            number = len(self.issues) + 1
            issue = {
                "id": 1000 + number,
                "number": number,
                "title": str(body["title"]),
                "body": body.get("body"),
                "state": "open",
                "html_url": f"https://github.example/{self.repo}/issues/{number}",
                "created_at": time.time(),
            }
            self.issues.append(issue)
            if fault == "500_after":
                return JSONResponse({"message": "Server Error"}, status_code=500)
            if fault == "timeout_after":  # committed; the reply comes after the client gave up
                while not self.release_held_replies.is_set():
                    await asyncio.sleep(0.05)
            return JSONResponse(issue, status_code=201)

        @app.get(f"/repos/{owner}/{name}/issues/{{number}}")
        async def get(number: int, request: Request) -> Any:
            if (bad := check(request)) is not None:
                return bad
            for i in self.issues:
                if i["number"] == number:
                    return i
            return JSONResponse({"message": "Not Found"}, status_code=404)

        @app.get(f"/repos/{owner}/{name}/issues")
        async def list_issues(request: Request) -> Any:
            if (bad := check(request)) is not None:
                return bad
            now = time.time()
            visible = [i for i in self.issues if now - i["created_at"] >= self.hide_recent_seconds]
            visible.append(
                {
                    "number": 999,
                    "title": "A pull request",
                    "body": "not an issue",
                    "pull_request": {},
                }
            )
            return list(reversed(visible))

        return app
