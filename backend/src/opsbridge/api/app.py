"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from opsbridge import __version__
from opsbridge.config import Settings, get_settings
from opsbridge.persistence.db import Database, get_database

from . import errors
from .deps import AppState
from .middleware import PerimeterMiddleware
from .routes import admin, auth, cases, evaluations, health, investigations, proposals


def create_app(
    settings: Settings | None = None, db: Database | None = None, static_dir: Path | None = None
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="OpsBridge API",
        version=__version__,
        description="Evidence-backed investigation of blocked procurement exceptions.",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        # FastAPI's native telemetry would trust incoming trace context and record raw URLs;
        # the perimeter middleware creates the server span instead (route templates, safe ids).
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    app.state.opsbridge = AppState(settings=settings, db=db or get_database("app"))
    _watch(app.state.opsbridge)
    app.state.oauth = auth.build_oauth(settings)
    errors.install(app)

    # Order: the last added middleware runs first.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Content-Type",
            "X-CSRF-Token",
            "Idempotency-Key",
            "If-Match",
            "X-Correlation-Id",
        ],
        max_age=600,
    )
    secret = settings.session_secret.get_secret_value() or "dev-only-insecure-login-state-secret"
    # Holds only transient OIDC login state (state, nonce, PKCE verifier) for 10 minutes.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="opsbridge_login",
        max_age=600,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.add_middleware(
        PerimeterMiddleware,
        max_body_bytes=settings.max_request_bytes,
        rate_limit_per_minute=settings.rate_limit_per_minute,
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(cases.router)
    app.include_router(investigations.router)
    app.include_router(proposals.router)
    app.include_router(admin.tenant_router)
    app.include_router(admin.case_router)
    app.include_router(evaluations.router)

    if static_dir is not None and static_dir.is_dir():
        _mount_spa(app, static_dir)
    return app


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")
    index = static_dir / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        return FileResponse(index)


def _watch(state: AppState) -> None:
    """Process gauges for this API: pool use, artifact storage, dependency readiness."""
    from sqlalchemy import text

    from opsbridge.telemetry import metrics

    metrics.POOLS.register("app", state.db.engine)
    metrics.watch_storage(state.settings.artifact_dir)

    def database() -> bool:
        with state.db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    def identity_provider() -> bool:
        import httpx

        base = (state.settings.oidc_discovery_url or state.settings.oidc_issuer).rstrip("/")
        return httpx.get(f"{base}/.well-known/openid-configuration", timeout=2.0).status_code == 200

    metrics.watch_dependencies({"database": database, "identity_provider": identity_provider})
