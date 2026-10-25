"""FastAPI dependencies: session, CSRF, tenant-scoped principal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import Depends, Path, Request
from sqlalchemy import select

from opsbridge.auth.permissions import TIERS, Principal, permissions_for
from opsbridge.auth.sessions import COOKIE_NAME, CSRF_HEADER, SessionInfo, load_session
from opsbridge.config import Settings
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Membership, Tenant
from opsbridge.telemetry import tracing

from .errors import ApiError, not_found, unauthenticated

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class AppState:
    settings: Settings
    db: Database
    user_limiter: Any = None  # per verified user; set by create_app


def app_state(request: Request) -> AppState:
    state: AppState = request.app.state.opsbridge
    return state


State = Annotated[AppState, Depends(app_state)]


def current_session(request: Request, state: State) -> SessionInfo:
    info = load_session(state.db, state.settings, request.cookies.get(COOKIE_NAME))
    if info is None:
        raise unauthenticated()
    if state.user_limiter is not None:  # only a verified session reaches its own budget
        allowed, retry_after = state.user_limiter.hit(f"u:{info.user_id}")
        if not allowed:
            raise ApiError(
                429,
                "rate_limited",
                "Too many requests; slow down.",
                retryable=True,
                headers={"Retry-After": str(retry_after)},
            )
    if request.method in UNSAFE_METHODS:
        _check_csrf(request, info, state.settings)
    request.state.session = info
    return info


def _check_csrf(request: Request, info: SessionInfo, settings: Settings) -> None:
    origin = request.headers.get("origin")
    if origin is not None:
        allowed = {_origin(settings.public_url), *(_origin(o) for o in settings.cors_origins)}
        if _origin(origin) not in allowed:
            raise ApiError(403, "csrf_rejected", "Cross-origin request rejected.")
    if not info.csrf_matches(request.headers.get(CSRF_HEADER)):
        raise ApiError(403, "csrf_rejected", "Missing or invalid CSRF token.")


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


CurrentSession = Annotated[SessionInfo, Depends(current_session)]


def resolve_principal(db: Database, info: SessionInfo, tenant_slug: str) -> Principal | None:
    """Build a principal from server-side membership. The slug is only a requested scope."""
    with db.session(DbContext(user_id=info.user_id)) as s:
        row = s.execute(
            select(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(
                Tenant.slug == tenant_slug,
                Membership.user_id == info.user_id,
                Membership.status == "active",
            )
        ).first()
        if row is None:
            return None
        tenant, membership = row
        return Principal(
            user_id=info.user_id,
            username=info.username,
            display_name=info.display_name,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            tenant_pack=tenant.pack_id,
            roles=tuple(membership.roles),
            approval_scope=tuple(membership.approval_scope or ()),
            tier=TIERS[tenant.tier],
            permissions=permissions_for(list(membership.roles)),
        )


def current_principal(
    info: CurrentSession,
    state: State,
    tenant: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")],
) -> Principal:
    principal = resolve_principal(state.db, info, tenant)
    if principal is None:
        raise not_found("tenant")
    tracing.set_ids(tenant_id=principal.tenant_id, user_id=principal.user_id)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def tenant_ctx(principal: Principal) -> DbContext:
    return DbContext(tenant_id=principal.tenant_id, user_id=principal.user_id)
