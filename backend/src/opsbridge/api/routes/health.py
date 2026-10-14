"""Liveness and readiness. Liveness = process up; readiness = dependencies usable."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..deps import State

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(state: State) -> Any:
    checks: dict[str, str] = {}
    try:
        with state.db.session() as s:
            s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error:{type(exc).__name__}"
    discovery = state.settings.oidc_discovery_url or state.settings.oidc_issuer
    try:
        r = httpx.get(f"{discovery.rstrip('/')}/.well-known/openid-configuration", timeout=2.0)
        checks["identity_provider"] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
    except httpx.HTTPError as exc:
        checks["identity_provider"] = f"error:{type(exc).__name__}"
    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503, content={"ready": ready, "checks": checks}
    )
