"""Idempotent seeding of the designated synthetic demo tenants only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from opsbridge.config import REPO_ROOT, Settings
from opsbridge.packs import load_policy_file
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import Membership, PolicyVersion, ServiceIdentity, Tenant, User

DEFAULT_FIXTURE = REPO_ROOT / "fixtures" / "demo" / "tenants.yaml"


class SeedError(RuntimeError):
    pass


@dataclass
class SeedReport:
    tenants_created: list[str] = field(default_factory=list)
    tenants_existing: list[str] = field(default_factory=list)
    users_upserted: int = 0
    memberships_upserted: int = 0
    policies_created: list[str] = field(default_factory=list)
    policies_existing: list[str] = field(default_factory=list)


def seed_demo(
    db: Database, settings: Settings, fixture: Path = DEFAULT_FIXTURE, issuer: str | None = None
) -> SeedReport:
    """Create/update designated demo tenants. `db` must use the migrator role."""
    spec: dict[str, Any] = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    iss = issuer or os.environ.get(spec.get("issuer_env", ""), "") or settings.oidc_issuer
    report = SeedReport()
    tenant_ids: dict[str, Any] = {}

    with db.session() as s:
        for sid in spec.get("service_identities", []):
            row = s.execute(
                select(ServiceIdentity).where(ServiceIdentity.client_id == sid["client_id"])
            ).scalar_one_or_none()
            if row is None:
                s.add(
                    ServiceIdentity(
                        client_id=sid["client_id"], kind=sid["kind"], description=sid["description"]
                    )
                )
        for t in spec["tenants"]:
            tenant = s.execute(select(Tenant).where(Tenant.slug == t["slug"])).scalar_one_or_none()
            if tenant is None:
                tenant = Tenant(
                    slug=t["slug"],
                    name=t["name"],
                    tier=t["tier"],
                    pack_id=t["pack"],
                    synthetic_demo=True,
                )
                s.add(tenant)
                s.flush()
                report.tenants_created.append(t["slug"])
            else:
                if not tenant.synthetic_demo:
                    raise SeedError(
                        f"tenant {t['slug']} exists and is not a synthetic demo tenant; refusing"
                    )
                tenant.name, tenant.tier, tenant.pack_id = t["name"], t["tier"], t["pack"]
                report.tenants_existing.append(t["slug"])
            tenant_ids[t["slug"]] = tenant.id
            for m in t["members"]:
                user = s.execute(
                    select(User).where(User.issuer == iss, User.subject == m["subject"])
                ).scalar_one_or_none()
                if user is None:
                    user = User(
                        issuer=iss,
                        subject=m["subject"],
                        username=m["username"],
                        display_name=m["display"],
                        email=f"{m['username']}@example.test",
                    )
                    s.add(user)
                    s.flush()
                report.users_upserted += 1
                mem = s.execute(
                    select(Membership).where(
                        Membership.tenant_id == tenant.id, Membership.user_id == user.id
                    )
                ).scalar_one_or_none()
                if mem is None:
                    mem = Membership(
                        tenant_id=tenant.id,
                        user_id=user.id,
                        roles=m["roles"],
                        approval_scope=m.get("approval_scope", []),
                        status="active",
                    )
                    s.add(mem)
                else:
                    mem.roles = m["roles"]
                    mem.approval_scope = m.get("approval_scope", [])
                    mem.status = "active"
                report.memberships_upserted += 1

    packs_dir = settings.packs_dir
    for t in spec["tenants"]:
        tid = tenant_ids[t["slug"]]
        with db.session(DbContext(tenant_id=tid)) as s:
            for rel in t["policies"]:
                loaded = load_policy_file(packs_dir / rel)
                p = loaded.policy
                existing = s.execute(
                    select(PolicyVersion).where(
                        PolicyVersion.policy_id == p.policy_id, PolicyVersion.version == p.version
                    )
                ).scalar_one_or_none()
                key = f"{t['slug']}:{p.ref}"
                if existing is None:
                    s.add(
                        PolicyVersion(
                            tenant_id=tid,
                            policy_id=p.policy_id,
                            version=p.version,
                            effective_from=p.effective_from,
                            document=loaded.document,
                            sha256=loaded.sha256,
                        )
                    )
                    report.policies_created.append(key)
                elif existing.sha256 != loaded.sha256:
                    raise SeedError(
                        f"{key} changed on disk; policy versions are immutable — publish a new version"
                    )
                else:
                    report.policies_existing.append(key)
    return report
