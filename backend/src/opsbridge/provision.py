"""Provision one organization's tenant from a reviewed spec file: never a demo tenant.

`make seed-demo` creates only the designated synthetic demo tenants, whose accounts share a
demo password. A pilot needs its own tenant instead (docs/pilot/quickstart.md, section 6).
This bootstraps one from a spec file (`opsbridge-tenant/1`, template
`fixtures/pilot/tenant.example.yaml`) and runs with the migrator role, from the host, by the
deployment's operator. It creates only what the application has no other way to create:
- the tenant, which is not flagged as a synthetic demo;
- its settings: the model mode, rules only by default;
- its first task connection: the simulator organization named after the tenant. With
  `OPSBRIDGE_OUTBOUND_WRITES=disabled` nothing is sent, and the administrator can change the
  connection in the app;
- its policy versions, each validated for the tenant's pack;
- its first administrators, by the subject their identity provider gives them.

Everyone else joins through the app's invitations (`services/invitations.py`), which an
administrator issues and an identity provider's verified email address accepts.

**Re-running is safe.** It publishes policy versions added to the spec, and it adds
administrators who are not members yet. It changes nothing that already exists:
- names, tiers and settings are changed in the app, where the change is audited;
- an existing membership is never reinstated or widened;
- a policy version is immutable, so a changed file with the same version number is refused.

Everything happens in one transaction. Any refusal leaves the database as it was. Every
creation is audited, with the spec file's digest. `--dry-run` validates and reports, then
rolls back.

**Refused:**
- a demo tenant's slug, or an existing demo tenant;
- a demo account's subject;
- another pack for an existing tenant;
- a policy for another pack, or a model mode or connector the tier does not include.
- a spec that still has the template's placeholders.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from opsbridge.audit import events as audit
from opsbridge.auth.permissions import TIERS
from opsbridge.config import REPO_ROOT, ModelMode
from opsbridge.packs import LoadedPolicy, PackRegistry, load_policy_file
from opsbridge.persistence.db import Database
from opsbridge.persistence.models import (
    Membership,
    PolicyVersion,
    TaskConnection,
    Tenant,
    TenantSettings,
    User,
)

SCHEMA = "opsbridge-tenant/1"
DEMO_FIXTURE = REPO_ROOT / "fixtures" / "demo" / "tenants.yaml"
ACTOR = "cli:tenant-provision"
PLACEHOLDER = "<<FILL"  # fixtures/pilot/tenant.example.yaml
_SLUG = r"^[a-z][a-z0-9-]{2,62}[a-z0-9]$"
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


class ProvisionError(RuntimeError):
    """The spec cannot be applied; nothing was written."""


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Admin(_M):
    subject: str = Field(min_length=1, max_length=300)  # the identity provider's `sub`
    username: str = Field(min_length=1, max_length=200)
    display: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=320)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _EMAIL.match(v):
            raise ValueError("not an email address")
        return v.strip().lower()


class TenantSpec(_M):
    schema_: Literal["opsbridge-tenant/1"] = Field(alias="schema")
    slug: str = Field(pattern=_SLUG)  # also the export folder: <export dir>/<slug>/
    name: str = Field(min_length=1, max_length=200)
    tier: Literal["standard", "enhanced"]
    pack: str = Field(min_length=1, max_length=64)
    model_mode: ModelMode = "rules_only"
    issuer: str = Field(pattern=r"^https?://", max_length=300)  # the admins' OIDC issuer
    policies: list[str] = Field(min_length=1)  # files, relative to the spec file
    admins: list[Admin] = Field(min_length=1)


@dataclass
class ProvisionReport:
    slug: str
    tenant_id: str | None = None
    dry_run: bool = False
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # already there; left exactly as it is

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "tenant_id": self.tenant_id,
            "dry_run": self.dry_run,
            "created": self.created,
            "kept": self.kept,
        }


class _DryRun(Exception):
    pass


def load_spec(path: Path) -> tuple[TenantSpec, list[LoadedPolicy], str]:
    """The spec, its policies loaded from files beside it, and the spec file's digest."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProvisionError(f"cannot read the spec {path}: {exc.strerror}") from exc
    if PLACEHOLDER.encode() in raw:
        raise ProvisionError(f"{path}: the template's {PLACEHOLDER}…>> values are not filled in")
    try:
        spec = TenantSpec.model_validate(yaml.safe_load(raw))
    except (ValidationError, yaml.YAMLError) as exc:
        raise ProvisionError(f"{path}: not a valid {SCHEMA} spec: {exc}") from exc
    policies = []
    for rel in spec.policies:
        file = (path.parent / rel).resolve()
        try:
            policies.append(load_policy_file(file))
        except (OSError, ValidationError, yaml.YAMLError) as exc:
            raise ProvisionError(f"policy {rel}: cannot be loaded: {exc}") from exc
    return spec, policies, "sha256:" + hashlib.sha256(raw).hexdigest()


def check_spec(
    spec: TenantSpec, policies: list[LoadedPolicy], packs: PackRegistry, demo: Path = DEMO_FIXTURE
) -> None:
    """Refusals that need no database."""
    demo_spec = yaml.safe_load(demo.read_text(encoding="utf-8"))
    demo_slugs = {t["slug"] for t in demo_spec["tenants"]}
    demo_subjects = {m["subject"] for t in demo_spec["tenants"] for m in t["members"]}
    if spec.slug in demo_slugs:
        raise ProvisionError(f"{spec.slug} is a synthetic demo tenant's slug; choose another")
    shared = sorted({a.subject for a in spec.admins} & demo_subjects)
    if shared:
        raise ProvisionError(
            f"admin subject(s) {', '.join(shared)} belong to demo accounts, which share a "
            "password; use the person's own account"
        )
    if len({a.subject for a in spec.admins}) != len(spec.admins):
        raise ProvisionError("an admin is listed twice")
    try:
        pack = packs.get(spec.pack)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise ProvisionError(f"unknown pack {spec.pack!r}") from exc
    entitled = TIERS[spec.tier]
    if spec.model_mode not in entitled.model_modes:
        raise ProvisionError(f"model mode {spec.model_mode!r} is not in the {spec.tier} tier")
    if "sim_tasks" not in entitled.external_connectors:
        raise ProvisionError(f"the {spec.tier} tier has no simulator task connector")
    versions = set()
    for loaded in policies:
        p = loaded.policy
        if p.policy_id != pack.policy_id:
            raise ProvisionError(
                f"policy {p.ref} is not for pack {spec.pack} (it uses {pack.policy_id})"
            )
        if p.version in versions:
            raise ProvisionError(f"policy {p.ref} is listed twice")
        versions.add(p.version)


def provision(
    db: Database,
    spec: TenantSpec,
    policies: list[LoadedPolicy],
    spec_digest: str,
    *,
    dry_run: bool = False,
) -> ProvisionReport:
    """Apply `spec` in one transaction. `db` must use the migrator role."""
    report = ProvisionReport(spec.slug, dry_run=dry_run)
    try:
        with db.session() as s:
            _apply(s, spec, policies, spec_digest, report)
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return report


def _apply(
    s: Session,
    spec: TenantSpec,
    policies: list[LoadedPolicy],
    spec_digest: str,
    report: ProvisionReport,
) -> None:
    tenant = s.execute(select(Tenant).where(Tenant.slug == spec.slug)).scalar_one_or_none()
    new = tenant is None
    if tenant is None:
        tenant = Tenant(
            slug=spec.slug, name=spec.name, tier=spec.tier, pack_id=spec.pack, synthetic_demo=False
        )
        s.add(tenant)
        s.flush()
    elif tenant.synthetic_demo:
        raise ProvisionError(f"{spec.slug} is a synthetic demo tenant; refusing")
    elif tenant.pack_id != spec.pack:
        raise ProvisionError(
            f"{spec.slug} uses pack {tenant.pack_id}; a tenant's pack is not changed here"
        )
    report.tenant_id = str(tenant.id)
    tid = tenant.id

    def record(action: str, resource_type: str, resource_id: str, **details: Any) -> None:
        audit.record(
            s,
            tenant_id=tid,
            actor_kind="system",
            actor_id=ACTOR,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details={"spec_sha256": spec_digest, **details},
        )

    # Tenant-scoped rows below need the tenant's context, in this same transaction.
    s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant.id)})

    if new:
        s.add(
            TenantSettings(
                tenant_id=tenant.id, model_mode=spec.model_mode, task_connector="sim_tasks"
            )
        )
        s.add(
            TaskConnection(
                tenant_id=tenant.id,
                version=1,
                connector="sim_tasks",
                destination={"connector": "sim_tasks", "organization": spec.slug},
                status="active",
                reason="provisioned (tenant-provision)",
            )
        )
        s.flush()
        record(
            "tenant.provisioned",
            "tenant",
            str(tenant.id),
            slug=spec.slug,
            tier=spec.tier,
            pack=spec.pack,
            model_mode=spec.model_mode,
        )
        report.created += [f"tenant {spec.slug}", "settings", "task connection sim_tasks v1"]
    else:
        report.kept.append(f"tenant {spec.slug} (name, tier and settings are changed in the app)")

    for loaded in policies:
        p = loaded.policy
        existing = s.execute(
            select(PolicyVersion).where(
                PolicyVersion.tenant_id == tenant.id,
                PolicyVersion.policy_id == p.policy_id,
                PolicyVersion.version == p.version,
            )
        ).scalar_one_or_none()
        if existing is None:
            pv = PolicyVersion(
                tenant_id=tenant.id,
                policy_id=p.policy_id,
                version=p.version,
                effective_from=p.effective_from,
                document=loaded.document,
                sha256=loaded.sha256,
            )
            s.add(pv)
            s.flush()
            record("policy.published", "policy_version", str(pv.id), ref=p.ref, sha256=pv.sha256)
            report.created.append(f"policy {p.ref}")
        elif existing.sha256 != loaded.sha256:
            raise ProvisionError(
                f"policy {p.ref} is already published with other content; policy versions are "
                "immutable, so publish the change as a new version"
            )
        else:
            report.kept.append(f"policy {p.ref}")

    for a in spec.admins:
        user = s.execute(
            select(User).where(User.issuer == spec.issuer, User.subject == a.subject)
        ).scalar_one_or_none()
        if user is None:
            user = User(
                issuer=spec.issuer,
                subject=a.subject,
                username=a.username,
                display_name=a.display,
                email=a.email,
            )
            s.add(user)
            s.flush()
        member = s.execute(
            select(Membership).where(
                Membership.tenant_id == tenant.id, Membership.user_id == user.id
            )
        ).scalar_one_or_none()
        if member is not None:
            report.kept.append(f"member {a.username} ({member.status}, roles as they are)")
            continue
        m = Membership(
            tenant_id=tenant.id,
            user_id=user.id,
            roles=["tenant_admin"],
            approval_scope=[],
            status="active",
            change_reason="provisioned (tenant-provision)",
        )
        s.add(m)
        s.flush()
        record("membership.granted", "membership", str(m.id), user_id=str(user.id), roles=m.roles)
        report.created.append(f"admin {a.username}")


def run(
    db: Database, spec_path: Path, packs: PackRegistry, *, dry_run: bool = False
) -> ProvisionReport:
    spec, policies, digest = load_spec(spec_path)
    check_spec(spec, policies, packs)
    return provision(db, spec, policies, digest, dry_run=dry_run)


__all__ = [
    "ProvisionError",
    "ProvisionReport",
    "TenantSpec",
    "check_spec",
    "load_spec",
    "provision",
    "run",
]
