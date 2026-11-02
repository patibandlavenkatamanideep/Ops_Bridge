"""OIDC Authorization Code + PKCE login, logout (here and from the identity provider), and the
current-user endpoint."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from opsbridge.auth.backchannel import (
    MAX_CLAIM_CHARS,
    LogoutTokenInvalid,
    UnknownSigningKey,
    verify_logout_token,
)
from opsbridge.auth.permissions import TIERS, permissions_for
from opsbridge.auth.sessions import (
    COOKIE_NAME,
    create_session,
    revoke_idp_sessions,
    revoke_session,
    upsert_login_user,
)
from opsbridge.config import Settings
from opsbridge.persistence.db import DbContext
from opsbridge.persistence.models import Membership, Tenant

from ..deps import CurrentSession, State
from ..errors import ApiError

log = logging.getLogger("opsbridge.auth")
router = APIRouter()

LOGIN_NEXT_KEY = "opsbridge_next"
# A logout token naming a key not yet fetched makes OpsBridge fetch the provider's keys again
# (it may have rotated them), but no more often than this, whatever is sent.
JWKS_REFETCH_SECONDS = 60.0


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
        email_verified=claims.get("email_verified") is True,  # only an explicit claim counts
    )
    sid = claims.get("sid")
    raw = await run_in_threadpool(
        create_session,
        state.db,
        settings,
        user_id,
        token.get("id_token"),
        sid if isinstance(sid, str) and 0 < len(sid) <= MAX_CLAIM_CHARS else None,
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
    response = JSONResponse({"logout_url": logout_url})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.post("/auth/backchannel-logout", include_in_schema=False)
async def backchannel_logout(request: Request, state: State) -> Any:
    """OpenID Connect Back-Channel Logout: the identity provider says one of its sessions has
    ended, and the OpsBridge sessions opened from it end too (opsbridge/auth/backchannel.py).
    Called by the provider, not a browser: no cookie, so no CSRF token."""
    settings = state.settings
    no_store = {"Cache-Control": "no-store"}
    client = request.app.state.oauth.opsbridge
    raw = (await request.form()).get("logout_token")

    def verify(jwks: dict[str, Any]) -> Any:
        if not isinstance(raw, str):
            raise LogoutTokenInvalid("no logout token")
        return verify_logout_token(
            raw, jwks, issuer=settings.oidc_issuer, audience=settings.oidc_client_id
        )

    try:
        try:
            claims = verify(await client.fetch_jwk_set())
        except UnknownSigningKey:
            last = getattr(request.app.state, "jwks_refetched_at", None)
            if last is not None and time.monotonic() - last < JWKS_REFETCH_SECONDS:
                raise
            request.app.state.jwks_refetched_at = time.monotonic()
            claims = verify(await client.fetch_jwk_set(force=True))
    except LogoutTokenInvalid as exc:
        log.info("back-channel logout refused: %s", exc)
        return JSONResponse({"error": "invalid_request"}, status_code=400, headers=no_store)
    except Exception as exc:  # the provider's keys could not be fetched
        log.warning("back-channel logout: provider keys unavailable: %s", type(exc).__name__)
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503, headers=no_store)
    ended = await run_in_threadpool(
        revoke_idp_sessions,
        state.db,
        issuer=settings.oidc_issuer,
        subject=claims.subject,
        sid=claims.sid,
    )
    log.info("back-channel logout ended %d session(s)", ended)
    return Response(status_code=200, headers=no_store)


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
