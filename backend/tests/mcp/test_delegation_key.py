"""Delegation signing key sources: injected secret (staging), file (development), and no
silent key generation in production."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from opsbridge.config import get_settings
from opsbridge.mcp_services.auth import DelegationError
from opsbridge.mcp_services.factory import delegation_key, signer, verifier


def _pem(key: object) -> str:
    return key.private_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def test_injected_secret_is_used_and_never_written(tmp_path: Path) -> None:
    key_file = tmp_path / "keys" / "delegation.pem"
    settings = get_settings().model_copy(
        update={
            "delegation_private_key": SecretStr(_pem(Ed25519PrivateKey.generate())),
            "delegation_key_path": key_file,
        }
    )
    tenant, user, case = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token = signer(settings).sign(
        tenant_id=tenant,
        principal_id=user,
        audience="opsbridge-records-mcp",
        tools=["get_invoice"],
        case_id=case,
        ttl_seconds=60,
    )
    claims = verifier(settings, "opsbridge-records-mcp").verify(token)
    assert claims.tenant_id == tenant and claims.principal_id == user
    assert not key_file.exists()


def test_non_ed25519_secret_is_refused() -> None:
    settings = get_settings().model_copy(
        update={"delegation_private_key": SecretStr(_pem(generate_private_key(SECP256R1())))}
    )
    with pytest.raises(DelegationError, match="not Ed25519"):
        delegation_key(settings)


def test_production_never_generates_a_key(tmp_path: Path) -> None:
    missing = tmp_path / "absent.pem"
    settings = get_settings().model_copy(
        update={"env": "production", "delegation_private_key": None, "delegation_key_path": missing}
    )
    with pytest.raises(DelegationError, match="secret store"):
        delegation_key(settings)
    assert not missing.exists()
    dev = settings.model_copy(update={"env": "development"})
    assert (
        isinstance(delegation_key(dev), Ed25519PrivateKey) and missing.exists()
    )  # development creates it
