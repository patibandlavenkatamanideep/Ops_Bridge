"""Provisioning one organization's tenant (provision.py) against real PostgreSQL.

What is written, that a refusal writes nothing, that a re-run changes nothing, and that the
provisioned administrator can work the tenant through the app while demo accounts cannot.
The organization, identity provider and people are invented; the demo's furniture policy
stands in for an organization's own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine, select

from opsbridge.config import REPO_ROOT
from opsbridge.packs import PackRegistry, load_policy_file
from opsbridge.persistence.db import Database, DbContext
from opsbridge.persistence.models import (
    AuditEvent,
    Membership,
    PolicyVersion,
    TaskConnection,
    Tenant,
    TenantSettings,
    User,
)
from opsbridge.provision import ProvisionError, ProvisionReport, load_spec, provision, run
from opsbridge.seed import seed_demo

from .test_api_cases import call, login

pytestmark = pytest.mark.integration
PACKS = PackRegistry(REPO_ROOT / "packs")
FURNITURE_V2 = REPO_ROOT / "packs" / "furniture" / "policies" / "furniture-ap-v2.yaml"
IT_V1 = REPO_ROOT / "packs" / "it_hardware" / "policies" / "it-procurement-v1.yaml"
SLUG = "northwind-pilot"
ISSUER = "https://idp.northwind.example/realms/pilot"
NORA = {
    "subject": "5f0c2b7e-0000-4000-8000-00000000a001",
    "username": "nw-admin",
    "display": "Nora Admin",
    "email": "nora.admin@northwind.example",
}
OMAR = {
    "subject": "5f0c2b7e-0000-4000-8000-00000000a002",
    "username": "nw-admin2",
    "display": "Omar Admin",
    "email": "omar.admin@northwind.example",
}


def spec(tmp_path: Path, name: str = "tenant.yaml", **changes: Any) -> Path:
    body: dict[str, Any] = {
        "schema": "opsbridge-tenant/1",
        "slug": SLUG,
        "name": "Northwind Traders (synthetic)",
        "tier": "standard",
        "pack": "furniture",
        "issuer": ISSUER,
        "policies": [str(FURNITURE_V2)],
        "admins": [NORA],
    }
    body.update(changes)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def apply(engine: Engine, path: Path, *, dry_run: bool = False) -> ProvisionReport:
    return run(Database(engine), path, PACKS, dry_run=dry_run)


def policy_version(tmp_path: Path, version: int, **changes: Any) -> Path:
    doc = yaml.safe_load(FURNITURE_V2.read_text(encoding="utf-8"))
    doc.update({"version": version, **changes})
    path = tmp_path / f"furniture-ap-v{version}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def state(engine: Engine, slug: str = SLUG) -> dict[str, Any] | None:
    db = Database(engine)
    with db.session() as s:
        tenant = s.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
        if tenant is None:
            return None
        tid = tenant.id
        members = [
            (u.issuer, u.subject, m.roles, m.status)
            for m, u in s.execute(
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .where(Membership.tenant_id == tid)
                .order_by(User.username)
            ).tuples()
        ]
        tenant_row = (tenant.name, tenant.tier, tenant.pack_id, tenant.synthetic_demo)
    with db.session(DbContext(tenant_id=tid)) as s:
        settings = s.get(TenantSettings, tid)
        assert settings is not None
        return {
            "tenant_id": tid,
            "tenant": tenant_row,
            "members": members,
            "model_mode": settings.model_mode,
            "connections": [
                (c.version, c.connector, c.destination, c.status)
                for c in s.execute(select(TaskConnection)).scalars()
            ],
            "policies": [
                (p.policy_id, p.version, p.sha256)
                for p in s.execute(select(PolicyVersion).order_by(PolicyVersion.version)).scalars()
            ],
            "audit": [
                (a.action, a.actor_kind, a.actor_id, a.details)
                for a in s.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars()
            ],
        }


def test_provisioning_creates_a_tenant_that_is_not_a_demo_with_its_policy_and_first_admin(
    stack: dict[str, Any], migrator_engine: Engine, tmp_path: Path
) -> None:
    path = spec(tmp_path)
    report = apply(migrator_engine, path)
    assert report.created == [
        f"tenant {SLUG}",
        "settings",
        "task connection sim_tasks v1",
        "policy furniture-ap@v2",
        "admin nw-admin",
    ]
    st = state(migrator_engine)
    assert st is not None and report.tenant_id == str(st["tenant_id"])
    assert st["tenant"] == ("Northwind Traders (synthetic)", "standard", "furniture", False)
    assert st["model_mode"] == "rules_only"
    assert st["connections"] == [
        (1, "sim_tasks", {"connector": "sim_tasks", "organization": SLUG}, "active")
    ]
    assert st["policies"] == [("furniture-ap", 2, load_policy_file(FURNITURE_V2).sha256)]
    assert st["members"] == [(ISSUER, NORA["subject"], ["tenant_admin"], "active")]
    _spec, _policies, digest = load_spec(path)
    assert [(a, k, i) for a, k, i, _d in st["audit"]] == [
        ("tenant.provisioned", "system", "cli:tenant-provision"),
        ("policy.published", "system", "cli:tenant-provision"),
        ("membership.granted", "system", "cli:tenant-provision"),
    ]
    assert all(d["spec_sha256"] == digest for *_x, d in st["audit"])


def test_the_admin_works_the_tenant_through_the_app_and_demo_accounts_cannot(
    stack: dict[str, Any], migrator_engine: Engine, app_db: Database, tmp_path: Path
) -> None:
    st = stack
    apply(migrator_engine, spec(tmp_path))
    with Database(migrator_engine).session() as s:
        uid = s.execute(select(User.id).where(User.subject == NORA["subject"])).scalar_one()
    nora = login(app_db, st["settings"], uid)
    base = f"/api/v1/tenants/{SLUG}"

    r = call(st["client"], nora, "GET", f"{base}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["tenant"]["slug"], body["tenant"]["synthetic_demo"]) == (SLUG, False)
    assert [(p["policy_id"], p["version"], p["state"]) for p in body["policies"]] == [
        ("furniture-ap", 2, "active")
    ]
    # Everyone else joins by invitation, through the app.
    r = call(
        st["client"],
        nora,
        "POST",
        f"{base}/invitations",
        json={"email": "rita.reviewer@northwind.example", "roles": ["investigator"]},
    )
    assert r.status_code == 201, r.text

    # A demo administrator, even of a demo tenant, has no access here.
    r = call(st["client"], st["actors"]["furn-admin"], "GET", f"{base}/settings")
    assert r.status_code in (403, 404), r.text


def test_re_running_changes_nothing_and_publishes_only_versions_added(
    stack: dict[str, Any], migrator_engine: Engine, tmp_path: Path
) -> None:
    apply(migrator_engine, spec(tmp_path))
    before = state(migrator_engine)

    # Another tier in the spec: reported as kept, not applied (tiers change in the app).
    again = apply(migrator_engine, spec(tmp_path, name="again.yaml", tier="enhanced"))
    assert again.created == []
    assert again.kept == [
        f"tenant {SLUG} (name, tier and settings are changed in the app)",
        "policy furniture-ap@v2",
        "member nw-admin (active, roles as they are)",
    ]
    assert state(migrator_engine) == before  # not even the tier

    v3 = policy_version(tmp_path, 3, effective_from="2026-12-01")
    added = apply(
        migrator_engine, spec(tmp_path, name="v3.yaml", policies=[str(FURNITURE_V2), str(v3)])
    )
    assert added.created == ["policy furniture-ap@v3"]
    after = state(migrator_engine)
    assert after is not None and [v for _p, v, _s in after["policies"]] == [2, 3]


def test_a_refusal_writes_nothing_at_all(
    stack: dict[str, Any], migrator_engine: Engine, tmp_path: Path
) -> None:
    apply(migrator_engine, spec(tmp_path))
    before = state(migrator_engine)
    # v2 again with other content, and a second admin in the same spec.
    changed = policy_version(tmp_path, 2, description="edited after publishing")
    with pytest.raises(ProvisionError, match="immutable"):
        apply(
            migrator_engine,
            spec(tmp_path, name="changed.yaml", policies=[str(changed)], admins=[NORA, OMAR]),
        )
    assert state(migrator_engine) == before
    with Database(migrator_engine).session() as s:
        assert s.execute(select(User.id).where(User.subject == OMAR["subject"])).all() == []


def test_a_demo_tenant_or_a_change_of_pack_is_refused_in_the_database_too(
    stack: dict[str, Any], migrator_engine: Engine, tmp_path: Path
) -> None:
    db = Database(migrator_engine)
    # check_spec would refuse the demo slug first; the database step refuses it again.
    demo_spec, demo_policies, digest = load_spec(spec(tmp_path, slug="demo-furniture"))
    with pytest.raises(ProvisionError, match="is a synthetic demo tenant; refusing"):
        provision(db, demo_spec, demo_policies, digest)

    apply(migrator_engine, spec(tmp_path))
    other, it_policies, digest = load_spec(
        spec(tmp_path, name="it.yaml", pack="it_hardware", policies=[str(IT_V1)])
    )
    with pytest.raises(ProvisionError, match="uses pack furniture"):
        provision(db, other, it_policies, digest)


def test_a_dry_run_reports_what_it_would_create_and_keeps_nothing(
    stack: dict[str, Any], migrator_engine: Engine, tmp_path: Path
) -> None:
    report = apply(migrator_engine, spec(tmp_path), dry_run=True)
    assert report.dry_run and report.created[0] == f"tenant {SLUG}"
    assert state(migrator_engine) is None


def test_seeding_the_demo_again_leaves_a_provisioned_tenant_alone(
    stack: dict[str, Any], migrator_engine: Engine, tmp_path: Path
) -> None:
    apply(migrator_engine, spec(tmp_path))
    before = state(migrator_engine)
    seed_demo(Database(migrator_engine), stack["settings"])
    assert state(migrator_engine) == before
