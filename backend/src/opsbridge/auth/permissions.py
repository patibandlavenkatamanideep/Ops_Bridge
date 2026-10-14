"""Role → permission mapping and the authenticated principal.

Authorization is explicit: roles grant named permissions; service tier never does.
Tenant administrators manage membership and configuration but do not read document
content or approve actions unless they also hold those roles.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["requester", "investigator", "approver", "tenant_admin", "auditor"]
ROLES: tuple[Role, ...] = ("requester", "investigator", "approver", "tenant_admin", "auditor")

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "requester": frozenset({"case.create", "case.read_own", "conversation.write"}),
    "investigator": frozenset(
        {
            "case.create",
            "case.read_all",
            "case.read_metadata",
            "case.assign",
            "evidence.read",
            "evidence.upload",
            "evidence.correct",
            "investigation.run",
            "proposal.read",
            "proposal.create",
            "operation.read",
            "conversation.write",
        }
    ),
    "approver": frozenset(
        {
            "case.read_all",
            "case.read_metadata",
            "evidence.read",
            "proposal.read",
            "proposal.decide",
            "operation.read",
            "operation.cancel",
        }
    ),
    "tenant_admin": frozenset(
        {
            "case.read_metadata",
            "tenant.read_settings",
            "tenant.manage_members",
            "tenant.manage_policy",
            "tenant.manage_connectors",
        }
    ),
    "auditor": frozenset(
        {
            "case.read_all",
            "case.read_metadata",
            "evidence.read",
            "proposal.read",
            "operation.read",
            "audit.read",
            "eval.read",
            "tenant.read_settings",
        }
    ),
}

# Permission each executable action requires from the approver (in addition to scope).
ACTION_PERMISSION = "proposal.decide"


def permissions_for(roles: list[str]) -> frozenset[str]:
    perms: set[str] = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(perms)


@dataclass(frozen=True)
class TierEntitlements:
    """Service-tier features. These enable capabilities; they never grant authority."""

    tier: str
    model_modes: frozenset[str]
    max_model_calls_per_run: int
    external_connectors: frozenset[str]
    local_inference: bool


TIERS: dict[str, TierEntitlements] = {
    "standard": TierEntitlements(
        tier="standard",
        model_modes=frozenset({"mock", "rules_only"}),
        max_model_calls_per_run=4,
        external_connectors=frozenset({"sim_tasks"}),
        local_inference=False,
    ),
    "enhanced": TierEntitlements(
        tier="enhanced",
        model_modes=frozenset({"mock", "rules_only", "anthropic", "ollama"}),
        max_model_calls_per_run=8,
        external_connectors=frozenset({"sim_tasks", "github_issues"}),
        local_inference=True,
    ),
}


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    username: str
    display_name: str
    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_pack: str
    roles: tuple[str, ...]
    approval_scope: tuple[str, ...]
    tier: TierEntitlements
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def can_approve(self, action_type: str) -> bool:
        return self.has("proposal.decide") and action_type in self.approval_scope


class Forbidden(Exception):
    def __init__(self, permission: str) -> None:
        super().__init__(f"missing permission {permission}")
        self.permission = permission


def require(principal: Principal, permission: str) -> None:
    if not principal.has(permission):
        raise Forbidden(permission)
