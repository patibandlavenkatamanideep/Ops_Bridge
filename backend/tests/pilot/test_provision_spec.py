"""The tenant spec (`opsbridge-tenant/1`) and what is refused before the database is touched.

The stack tests (tests/integration/test_tenant_provision.py) cover what is written. Specs here
are synthetic: an invented organization and identity provider, with the demo's furniture policy
standing in for an organization's own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from opsbridge.cli import build_parser
from opsbridge.config import REPO_ROOT
from opsbridge.packs import PackRegistry
from opsbridge.provision import ProvisionError, check_spec, load_spec

PACKS = PackRegistry(REPO_ROOT / "packs")
FURNITURE_V2 = REPO_ROOT / "packs" / "furniture" / "policies" / "furniture-ap-v2.yaml"
IT_V1 = REPO_ROOT / "packs" / "it_hardware" / "policies" / "it-procurement-v1.yaml"


def spec(tmp_path: Path, **changes: Any) -> Path:
    body: dict[str, Any] = {
        "schema": "opsbridge-tenant/1",
        "slug": "northwind-pilot",
        "name": "Northwind Traders (synthetic)",
        "tier": "standard",
        "pack": "furniture",
        "model_mode": "rules_only",
        "issuer": "https://idp.northwind.example/realms/pilot",
        "policies": [str(FURNITURE_V2)],
        "admins": [
            {
                "subject": "5f0c2b7e-0000-4000-8000-00000000a001",
                "username": "nw-admin",
                "display": "Nora Admin",
                "email": "Nora.Admin@northwind.example",
            }
        ],
    }
    body.update(changes)
    path = tmp_path / "tenant.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def checked(path: Path) -> None:
    s, policies, _digest = load_spec(path)
    check_spec(s, policies, PACKS)


def test_a_filled_spec_loads_with_its_policy_and_a_digest(tmp_path: Path) -> None:
    s, policies, digest = load_spec(spec(tmp_path))
    check_spec(s, policies, PACKS)
    assert (s.slug, s.model_mode, s.admins[0].email) == (
        "northwind-pilot",
        "rules_only",
        "nora.admin@northwind.example",
    )
    assert [p.policy.ref for p in policies] == ["furniture-ap@v2"]
    assert digest.startswith("sha256:") and len(digest) == 71


def test_a_policy_path_is_relative_to_the_spec_file(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "ap.yaml").write_bytes(FURNITURE_V2.read_bytes())
    s, policies, _ = load_spec(spec(tmp_path, policies=["policies/ap.yaml"]))
    assert policies[0].policy.ref == "furniture-ap@v2"


def test_the_shipped_template_is_refused_until_it_is_filled_in() -> None:
    template = REPO_ROOT / "fixtures" / "pilot" / "tenant.example.yaml"
    with pytest.raises(ProvisionError, match="<<FILL"):
        load_spec(template)


def test_a_name_that_merely_contains_replace_is_not_a_placeholder(tmp_path: Path) -> None:
    checked(spec(tmp_path, name="Replacement Parts Inc (synthetic)"))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"slug": "demo-furniture"}, "synthetic demo tenant's slug"),
        (
            {
                "admins": [
                    {
                        "subject": "11111111-0000-4000-8000-000000000005",  # furn-admin
                        "username": "x",
                        "display": "X",
                        "email": "x@northwind.example",
                    }
                ]
            },
            "belong to demo accounts",
        ),
        ({"pack": "no_such_pack"}, "unknown pack"),
        ({"policies": [str(IT_V1)]}, "is not for pack furniture"),
        ({"policies": [str(FURNITURE_V2), str(FURNITURE_V2)]}, "listed twice"),
        ({"model_mode": "anthropic"}, "not in the standard tier"),
    ],
)
def test_refused_before_the_database(tmp_path: Path, changes: Any, message: str) -> None:
    with pytest.raises(ProvisionError, match=message):
        checked(spec(tmp_path, **changes))


def test_the_same_admin_twice_is_refused(tmp_path: Path) -> None:
    admin = {
        "subject": "5f0c2b7e-0000-4000-8000-00000000a001",
        "username": "nw-admin",
        "display": "Nora Admin",
        "email": "nora@northwind.example",
    }
    with pytest.raises(ProvisionError, match="listed twice"):
        checked(spec(tmp_path, admins=[admin, admin]))


@pytest.mark.parametrize(
    "changes",
    [
        {"slug": "Northwind"},  # upper case
        {"slug": "nw"},  # too short
        {"tier": "platinum"},
        {"issuer": "idp.northwind.example"},  # not a URL
        {"admins": []},
        {"policies": []},
        {"synthetic_demo": True},  # not a field: nobody provisions a demo tenant
        {"schema": "opsbridge-tenant/2"},
        {"admins": [{"subject": "s", "username": "u", "display": "D", "email": "not-an-address"}]},
    ],
)
def test_an_invalid_spec_is_refused(tmp_path: Path, changes: Any) -> None:
    with pytest.raises(ProvisionError, match="not a valid opsbridge-tenant/1 spec"):
        load_spec(spec(tmp_path, **changes))


def test_a_missing_spec_file_is_refused_not_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(ProvisionError, match="cannot read the spec"):
        load_spec(tmp_path / "missing.yaml")


def test_a_missing_policy_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProvisionError, match="cannot be loaded"):
        load_spec(spec(tmp_path, policies=["missing.yaml"]))


def test_the_command_takes_a_spec_and_a_dry_run() -> None:
    args = build_parser().parse_args(["tenant-provision", "--spec", "t.yaml", "--dry-run"])
    assert (args.spec, args.dry_run, args.func.__name__) == ("t.yaml", True, "_tenant_provision")
