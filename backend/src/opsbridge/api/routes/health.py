"""Liveness and readiness. Liveness = process up; readiness = dependencies usable."""

from __future__ import annotations

import hmac
from typing import Any

import httpx
from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from opsbridge.telemetry import metrics

from ..deps import State

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics(state: State, authorization: str | None = Header(default=None)) -> Response:
    """Prometheus exposition: aggregate, bounded labels, no tenant data. Optionally token-gated;
    in staging it is reachable only on the private network (docs/deployment.md)."""
    expected = (
        state.settings.metrics_token.get_secret_value() if state.settings.metrics_token else ""
    )
    if expected and not hmac.compare_digest(authorization or "", f"Bearer {expected}"):
        return Response(status_code=401)
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE_LATEST)


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
