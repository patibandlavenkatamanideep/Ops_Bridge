"""Membership invitations: how a tenant administrator adds a person.

The administrator invites an email address with roles and gets a single-use link to pass on
(OpsBridge sends no email). The link carries a random token; only its SHA-256 is stored. A
signed-in person can accept it once, before it expires (database clock), and only if their
identity provider verified the invited address at their latest sign-in, so a forwarded link
is useless to anyone else. The inviting administrator must still be allowed to manage members
when the invitation is accepted, just as an approver must still hold authority at dispatch.
Accepting reinstates a revoked membership with the invited roles; an active member cannot
accept. Membership changes in a tenant run one at a time (casework_admin.lock_members).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import Principal, require
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Membership, MembershipInvitation, Tenant, User

from .casework_admin import (
    _settings_view,
    can_manage_members,
    check_grant,
    lock_members,
    lock_members_as_admin,
)
from .investigations import Conflict, NotFound

_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+")


class NotForThisAccount(Exception):
    """The signed-in identity may not accept this invitation (403)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > 320 or not _EMAIL.fullmatch(email):
        raise Conflict("invalid_email", "Enter one email address.")
    return email


def _ctx(p: Principal) -> DbContext:
    return DbContext(tenant_id=p.tenant_id, user_id=p.user_id)


def _open_invitations(s: Session, tenant_id: uuid.UUID) -> list[MembershipInvitation]:
    return list(
        s.execute(
            select(MembershipInvitation).where(
                MembershipInvitation.tenant_id == tenant_id,
                MembershipInvitation.status == "pending",
                MembershipInvitation.expires_at > func.now(),
            )
        ).scalars()
    )


def create_invitation(
    db: Database,
    p: Principal,
    *,
    email: str,
    roles: list[str],
    approval_scope: list[str],
    ttl_hours: int,
    max_open: int,
    correlation_id: str | None,
) -> tuple[dict[str, Any], str, MembershipInvitation]:
    """Return the settings view, the token (shown once, never stored) and the invitation."""
    require(p, "tenant.manage_members")
    address = normalize_email(email)
    roles, scope = sorted(set(roles)), sorted(set(approval_scope))
    check_grant(roles, scope)
    with db.session(_ctx(p)) as s:
        lock_members_as_admin(s, p)
        active = s.execute(
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == p.tenant_id,
                Membership.status == "active",
                func.lower(User.email) == address,
            )
        ).first()
        if active is not None:
            raise Conflict("already_member", "A person with this address is already a member.")
        open_ = _open_invitations(s, p.tenant_id)
        if any(i.email == address for i in open_):
            raise Conflict(
                "already_invited",
                "This address already has an open invitation; withdraw it to send a new one.",
            )
        if len(open_) >= max_open:
            raise Conflict("too_many_invitations", f"At most {max_open} invitations can be open.")
        token = secrets.token_urlsafe(32)
        inv = MembershipInvitation(
            tenant_id=p.tenant_id,
            email=address,
            roles=roles,
            approval_scope=scope,
            token_hash=token_hash(token),
            status="pending",
            invited_by=p.user_id,
            expires_at=func.now() + timedelta(hours=ttl_hours),
        )
        s.add(inv)
        s.flush()
        s.refresh(inv)
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="tenant.invitation_created",
            resource_type="membership_invitation",
            resource_id=str(inv.id),
            correlation_id=correlation_id,
            details={  # the address stays on the invitation row
                "roles": roles,
                "approval_scope": scope,
                "expires_at": inv.expires_at.isoformat(),
            },
        )
        return _settings_view(s, p.tenant_id), token, inv


def revoke_invitation(
    db: Database, p: Principal, invitation_id: uuid.UUID, correlation_id: str | None
) -> dict[str, Any]:
    require(p, "tenant.manage_members")
    with db.session(_ctx(p)) as s:
        lock_members_as_admin(s, p)
        inv = s.execute(
            select(MembershipInvitation)
            .where(
                MembershipInvitation.id == invitation_id,
                MembershipInvitation.tenant_id == p.tenant_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if inv is None:
            raise NotFound("invitation")
        if inv.status != "pending":
            raise Conflict("invitation_closed", f"This invitation was already {inv.status}.")
        inv.status, inv.closed_at = "revoked", func.now()
        audit.record(
            s,
            tenant_id=p.tenant_id,
            actor_kind="user",
            actor_id=str(p.user_id),
            action="tenant.invitation_revoked",
            resource_type="membership_invitation",
            resource_id=str(inv.id),
            correlation_id=correlation_id,
            details={},
        )
        s.flush()
        return _settings_view(s, p.tenant_id)


def accept_invitation(
    db: Database, user_id: uuid.UUID, token: str, correlation_id: str | None
) -> dict[str, Any]:
    if not token or len(token) > 100:
        raise NotFound("invitation")
    h = token_hash(token)
    # 1. Find the tenant: outside any tenant, RLS shows only the invitation with this hash.
    with db.session(DbContext(user_id=user_id, invite_token_hash=h)) as s:
        tenant_id = s.execute(
            select(MembershipInvitation.tenant_id).where(MembershipInvitation.token_hash == h)
        ).scalar_one_or_none()
    if tenant_id is None:
        raise NotFound("invitation")
    # 2. Decide and apply in that tenant's context, with its memberships locked.
    with db.session(DbContext(tenant_id=tenant_id, user_id=user_id)) as s:
        rows = lock_members(s, tenant_id)
        inv = s.execute(
            select(MembershipInvitation)
            .where(
                MembershipInvitation.token_hash == h,
                MembershipInvitation.tenant_id == tenant_id,
            )
            .with_for_update()
        ).scalar_one()
        if inv.status != "pending":
            raise Conflict("invitation_closed", "This invitation was already used or withdrawn.")
        if s.execute(select(func.now() >= inv.expires_at)).scalar_one():
            raise Conflict("invitation_expired", "This invitation has expired; ask for a new one.")
        inviter = next((m for m in rows if m.user_id == inv.invited_by), None)
        if not can_manage_members(inviter):
            raise Conflict(
                "inviter_not_authorized",
                "The administrator who invited you can no longer add members; ask for a new "
                "invitation.",
            )
        user = s.get(User, user_id)
        assert user is not None
        if not user.email_verified:
            raise NotForThisAccount(
                "email_not_verified",
                "Your identity provider has not verified your email address, so this "
                "invitation cannot be accepted.",
            )
        if (user.email or "").strip().lower() != inv.email:
            raise NotForThisAccount(
                "not_for_this_account",
                "This invitation is for a different email address. Sign in with the invited "
                "account.",
            )
        mine = next((m for m in rows if m.user_id == user_id), None)
        if mine is not None and mine.status == "active":
            raise Conflict("already_member", "You are already a member of this organization.")
        if mine is None:
            mine = Membership(tenant_id=tenant_id, user_id=user_id)
            s.add(mine)
        reinstated = mine.status == "revoked"
        mine.roles, mine.approval_scope, mine.status = (
            list(inv.roles),
            list(inv.approval_scope),
            "active",
        )
        mine.updated_by = inv.invited_by
        mine.change_reason = "Rejoined by invitation" if reinstated else "Joined by invitation"
        inv.status, inv.accepted_by, inv.closed_at = "accepted", user_id, func.now()
        s.flush()
        audit.record(
            s,
            tenant_id=tenant_id,
            actor_kind="user",
            actor_id=str(user_id),
            action="tenant.invitation_accepted",
            resource_type="membership_invitation",
            resource_id=str(inv.id),
            correlation_id=correlation_id,
            details={
                "membership_id": str(mine.id),
                "reinstated": reinstated,
                "roles": list(inv.roles),
            },
        )
        tenant = s.get(Tenant, tenant_id)
        assert tenant is not None
        return {"tenant_slug": tenant.slug, "tenant_name": tenant.name, "roles": list(inv.roles)}
