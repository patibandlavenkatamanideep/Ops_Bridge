"""OIDC Authorization Code + PKCE login, logout, and the current-user endpoint."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from opsbridge.auth.permissions import TIERS, permissions_for
from opsbridge.auth.sessions import COOKIE_NAME, create_session, revoke_session, upsert_login_user
from opsbridge.config import Settings
from opsbridge.persistence.db import DbContext
from opsbridge.persistence.models import Membership, Tenant

from ..deps import CurrentSession, State
from ..errors import ApiError

log = logging.getLogger("opsbridge.auth")
router = APIRouter()

LOGIN_NEXT_KEY = "opsbridge_next"


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    discovery = settings.oidc_discovery_url or settings.oidc_issuer
    oauth.register(
        name="opsbridge",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        server_metadata_url=f"{discovery.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email",
            "code_challenge_method": "S256",
            "timeout": 10,
        },
    )
    return oauth


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value[:500]
    return "/"


@router.get("/auth/login", include_in_schema=False)
async def login(request: Request, state: State, next: str | None = None) -> Any:
    request.session[LOGIN_NEXT_KEY] = _safe_next(next)
    client = request.app.state.oauth.opsbridge
    redirect_uri = f"{state.settings.public_url.rstrip('/')}/auth/callback"
    try:
        return await client.authorize_redirect(request, redirect_uri)
    except Exception as exc:  # discovery failure etc.
        log.warning("identity provider unavailable: %s", type(exc).__name__)
        raise ApiError(
            503, "dependency_unavailable", "The identity provider is unavailable.", retryable=True
        ) from exc


@router.get("/auth/callback", include_in_schema=False)
async def callback(request: Request, state: State) -> Any:
    settings = state.settings
    client = request.app.state.oauth.opsbridge
    try:
        token = await client.authorize_access_token(
            request,
            claims_options={"iss": {"essential": True, "value": settings.oidc_issuer}},
            leeway=30,
        )
    except OAuthError as exc:
        log.info("login rejected: %s", exc.error)
        raise ApiError(401, "login_failed", "Sign-in could not be completed.") from exc
    claims = token.get("userinfo")
    if not claims or claims.get("iss") != settings.oidc_issuer or not claims.get("sub"):
        raise ApiError(401, "login_failed", "Sign-in could not be completed.")
    username = str(claims.get("preferred_username") or claims["sub"])[:200]
    display = str(claims.get("name") or username)[:200]
    email = claims.get("email")
    user_id = await run_in_threadpool(
        upsert_login_user,
        state.db,
        issuer=settings.oidc_issuer,
        subject=str(claims["sub"]),
        username=username,
        display_name=display,
        email=str(email)[:320] if email else None,
    )
    raw = await run_in_threadpool(
        create_session, state.db, settings, user_id, token.get("id_token")
    )
    target = _safe_next(request.session.pop(LOGIN_NEXT_KEY, "/"))
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        raw,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    return response


class LogoutResponse(BaseModel):
    logout_url: str | None


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(request: Request, info: CurrentSession, state: State) -> Any:
    await run_in_threadpool(revoke_session, state.db, info.session_hash)
    logout_url: str | None = None
    try:
        metadata = await request.app.state.oauth.opsbridge.load_server_metadata()
        end = metadata.get("end_session_endpoint")
        if end and info.id_token:
            logout_url = (
                end
                + "?"
                + urlencode(
                    {
                        "id_token_hint": info.id_token,
                        "post_logout_redirect_uri": state.settings.public_url.rstrip("/") + "/",
                    }
                )
            )
    except Exception:  # IdP unreachable: local session is already revoked
        logout_url = None
    from fastapi.responses import JSONResponse

    response = JSONResponse({"logout_url": logout_url})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


class MembershipOut(BaseModel):
    tenant_slug: str
    tenant_name: str
    tier: str
    pack_id: str
    roles: list[str]
    approval_scope: list[str]
    permissions: list[str]
    synthetic_demo: bool
    model_modes: list[str]


class MeOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    csrf_token: str
    session_expires_at: str
    session_idle_expires_at: str
    memberships: list[MembershipOut]


@router.get("/api/v1/me", response_model=MeOut, tags=["session"])
def me(info: CurrentSession, state: State) -> MeOut:
    with state.db.session(DbContext(user_id=info.user_id)) as s:
        rows = s.execute(
            select(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Membership.user_id == info.user_id, Membership.status == "active")
            .order_by(Tenant.slug)
        ).all()
        memberships = [
            MembershipOut(
                tenant_slug=t.slug,
                tenant_name=t.name,
                tier=TIERS[t.tier].tier,
                pack_id=t.pack_id,
                roles=list(m.roles),
                approval_scope=list(m.approval_scope or []),
                permissions=sorted(permissions_for(list(m.roles))),
                synthetic_demo=t.synthetic_demo,
                model_modes=["rules_only", *sorted(TIERS[t.tier].model_modes - {"rules_only"})],
            )
            for t, m in rows
        ]
    return MeOut(
        user_id=str(info.user_id),
        username=info.username,
        display_name=info.display_name,
        csrf_token=info.csrf_token,
        session_expires_at=info.expires_at.isoformat(),
        session_idle_expires_at=info.idle_expires_at.isoformat(),
        memberships=memberships,
    )
