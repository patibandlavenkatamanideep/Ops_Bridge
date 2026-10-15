"""Deterministic purchasing-system simulator with fault injection.

This stands in for an external purchasing system and task tracker. It is a separate
HTTP service with its own SQLite store and its own credentials; OpsBridge reaches it
only through connectors. It is not an ERP integration.

Authentication: `Authorization: Bearer <org key>` where the org key is
HMAC-SHA256(master secret, org). A key works for exactly one org.

Fault injection (`/_control/*`) exists only when explicitly enabled and is refused in
production mode. Task-creation error semantics are part of the connector contract:
- 400/401/403/404/409/422: definite failure, nothing committed.
- 503 with `X-Sim-Not-Committed: true`: definite, retryable, nothing committed.
- 500/502/504, timeouts and dropped responses: ambiguous — the task may exist.
"""

import asyncio
import hashlib
import hmac
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .store import SimStore

ORG_RE = r"^[a-z0-9-]{1,64}$"
KEY_RE = r"^[A-Za-z0-9._-]{1,120}$"
COLLECTIONS = ("purchase_orders", "invoices", "receipts", "allocations", "messages", "suppliers")
FaultMode = Literal[
    "delay",
    "fail_500",
    "fail_400",
    "fail_503_not_committed",
    "malformed",
    "timeout_after_commit",
    "fail_500_after_commit",
]


def org_key(master: str, org: str) -> str:
    return hmac.new(master.encode(), f"opsbridge-sim:{org}".encode(), hashlib.sha256).hexdigest()


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20_000)
    assignee_group: str | None = Field(default=None, max_length=64)
    marker: str | None = Field(default=None, max_length=120)


class FaultIn(BaseModel):
    org: str = Field(pattern=ORG_RE)
    route: Literal["tasks.create", "tasks.get", "tasks.search", "records.get", "records.list"]
    mode: FaultMode
    count: int = Field(default=1, ge=1, le=100)
    delay_ms: int = Field(default=0, ge=0, le=120_000)


class CapabilitiesIn(BaseModel):
    org: str = Field(pattern=ORG_RE)
    idempotency: bool
    search: bool


def create_simulator(
    db_path: Path,
    master_secret: str,
    *,
    fault_controls: bool,
    production: bool,
    slow_response_seconds: float = 30.0,
) -> FastAPI:
    if production and fault_controls:
        raise RuntimeError("simulator fault controls cannot be enabled in production mode")
    if not master_secret:
        raise RuntimeError("simulator master secret is required")
    store = SimStore(db_path)
    app = FastAPI(
        title="OpsBridge purchasing simulator",
        version="1.0.0",
        description="Synthetic external purchasing system and task tracker for local testing.",
    )
    app.state.store = store

    def authorize(org: str, authorization: Annotated[str | None, Header()] = None) -> str:
        expected = org_key(master_secret, org)
        presented = (authorization or "").removeprefix("Bearer ").strip()
        if not presented or not hmac.compare_digest(presented, expected):
            raise HTTPException(status_code=401, detail="invalid credentials for this org")
        return org

    Org = Annotated[str, Depends(authorize)]

    async def apply_fault(org: str, route: str) -> str | None:
        fault = store.take_fault(org, route)
        if fault is None:
            return None
        if fault.mode == "delay":
            await asyncio.sleep(fault.delay_ms / 1000)
            return None
        return fault.mode

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/orgs/{org}/tasks", status_code=201)
    async def create_task(
        org: Org,
        body: TaskIn,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=120)
        ] = None,
    ) -> Any:
        caps = store.capabilities(org)
        key = idempotency_key if caps["idempotency"] else None
        mode = await apply_fault(org, "tasks.create")
        if mode == "fail_400":
            store.log(org, "tasks.create", "fault_400")
            return JSONResponse({"error": "rejected by validation"}, status_code=400)
        if mode == "fail_503_not_committed":
            store.log(org, "tasks.create", "fault_503")
            return JSONResponse(
                {"error": "unavailable"},
                status_code=503,
                headers={"X-Sim-Not-Committed": "true", "Retry-After": "1"},
            )
        if mode == "fail_500":  # nothing committed, but the client cannot know that
            store.log(org, "tasks.create", "fault_500_before_commit")
            return JSONResponse({"error": "internal"}, status_code=500)
        task, created = store.create_task(
            org, body.title, body.body, body.assignee_group, body.marker, key
        )
        if mode == "fail_500_after_commit":
            store.log(org, "tasks.create", "fault_500_after_commit")
            return JSONResponse({"error": "internal"}, status_code=500)
        if mode == "timeout_after_commit":
            store.log(org, "tasks.create", "timeout_after_commit")
            await asyncio.sleep(slow_response_seconds)
        else:
            store.log(org, "tasks.create", "created" if created else "idempotent_replay")
        return JSONResponse(task, status_code=201 if created else 200)

    @app.get("/orgs/{org}/tasks/{task_id}")
    async def get_task(org: Org, task_id: str) -> Any:
        mode = await apply_fault(org, "tasks.get")
        if mode == "fail_500":
            return JSONResponse({"error": "internal"}, status_code=500)
        task = store.get_task(org, task_id)
        if task is None:
            raise HTTPException(404, "not found")
        return task

    @app.get("/orgs/{org}/capabilities")
    async def capabilities(org: Org) -> dict[str, Any]:
        """What the task tracker honours right now (connectors discover this; never assume)."""
        caps = store.capabilities(org)
        return {
            "idempotency_keys": caps["idempotency"],
            "lookup": "strong" if caps["search"] else "none",
        }

    @app.get("/orgs/{org}/tasks")
    async def search_tasks(
        org: Org, marker: Annotated[str, Query(min_length=1, max_length=120)]
    ) -> Any:
        if not store.capabilities(org)["search"]:
            return JSONResponse({"error": "search not supported"}, status_code=501)
        mode = await apply_fault(org, "tasks.search")
        if mode == "fail_500":
            return JSONResponse({"error": "internal"}, status_code=500)
        return {"items": store.find_tasks(org, marker)}

    # Generic record routes are registered AFTER the task routes so /orgs/{org}/tasks/... is
    # never captured by /orgs/{org}/{collection}/{key}.
    @app.get("/orgs/{org}/{collection}")
    async def list_records(
        org: Org, collection: str, po_number: Annotated[str | None, Query(max_length=100)] = None
    ) -> Any:
        if collection not in COLLECTIONS:
            raise HTTPException(404, "unknown collection")
        mode = await apply_fault(org, "records.list")
        if mode == "fail_500":
            store.log(org, "records.list", "fault_500")
            return JSONResponse({"error": "internal"}, status_code=500)
        return {"items": store.list_records(org, collection, po_number)}

    @app.get("/orgs/{org}/{collection}/{key}")
    async def get_record(org: Org, collection: str, key: str) -> Response:
        if collection not in COLLECTIONS or not _valid_key(key):
            raise HTTPException(404, "not found")
        mode = await apply_fault(org, "records.get")
        if mode == "fail_500":
            store.log(org, "records.get", "fault_500")
            return JSONResponse({"error": "internal"}, status_code=500)
        rec = store.get_record(org, collection, key)
        if rec is None:
            raise HTTPException(404, "not found")
        body, ctype, digest = rec
        if mode == "malformed":
            body = body[: max(1, len(body) // 3)]
        store.log(org, "records.get", "ok")
        return Response(content=body, media_type=ctype, headers={"ETag": f'"{digest}"'})

    if fault_controls:

        @app.post("/_control/faults")
        def add_fault(body: FaultIn) -> dict[str, str]:
            return {
                "id": store.add_fault(body.org, body.route, body.mode, body.count, body.delay_ms)
            }

        @app.delete("/_control/faults")
        def clear_faults(org: str | None = None) -> dict[str, str]:
            store.clear_faults(org)
            return {"status": "cleared"}

        @app.post("/_control/capabilities")
        def set_caps(body: CapabilitiesIn) -> dict[str, str]:
            store.set_capabilities(body.org, body.idempotency, body.search)
            return {"status": "ok"}

        @app.get("/_control/stats/{org}")
        def stats(org: str) -> dict[str, Any]:
            return store.stats(org)

        @app.put("/_control/records/{org}/{collection}/{key}")
        async def put_record(
            org: str, collection: str, key: str, request: Request, po_number: str | None = None
        ) -> dict[str, str]:
            if collection not in COLLECTIONS or not _valid_key(key):
                raise HTTPException(404, "unknown collection")
            body = await request.body()
            ctype = request.headers.get("content-type", "application/octet-stream")
            return {"sha256": store.put_record(org, collection, key, body, ctype, po_number)}

    return app


def _valid_key(key: str) -> bool:
    return bool(re.match(KEY_RE, key))


def app_from_env() -> FastAPI:
    """Factory for `uvicorn --factory` and the CLI."""
    from opsbridge.config import REPO_ROOT, get_settings

    settings = get_settings()
    db = Path(os.environ.get("OPSBRIDGE_SIM_DB", str(REPO_ROOT / "var" / "simulator.sqlite3")))
    return create_simulator(
        db,
        settings.sim_master_secret.get_secret_value(),
        fault_controls=settings.simulator_fault_controls,
        production=settings.env == "production",
    )
