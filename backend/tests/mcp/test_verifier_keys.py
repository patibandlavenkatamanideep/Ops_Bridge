"""Verification-only services hold public keys only; signing keys rotate with an overlap
(production review A06).

Before, building a delegation verifier loaded the private signing key and took its public
half, so every MCP service that only verifies held a key that can mint delegations. Now a
verifier is built from configured public keys (a deployed service without them refuses to
start), delegations name their key, and several public keys can be accepted while keys
rotate. Development alone may derive the public key from the local key file.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from opsbridge.config import get_settings
from opsbridge.mcp_services.auth import (
    DELEGATION_ISSUER,
    DelegationError,
    DelegationSigner,
    DelegationVerifier,
)
from opsbridge.mcp_services.factory import signer, verifier

AUD = "opsbridge-records-mcp"


def _pub(key: Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _token(key: Ed25519PrivateKey) -> str:
    return DelegationSigner(key).sign(
        tenant_id=uuid.uuid4(), principal_id=uuid.uuid4(), audience=AUD, tools=["get_invoice"]
    )


def test_a_verification_only_service_verifies_with_the_public_key_and_cannot_sign(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()
    missing = tmp_path / "keys" / "delegation.pem"
    settings = get_settings().model_copy(
        update={
            "env": "staging",
            "delegation_private_key": None,
            "delegation_key_path": missing,
            "delegation_public_keys": [_pub(key)],
        }
    )
    claims = verifier(settings, AUD).verify(_token(key))
    assert claims.audience == AUD
    with pytest.raises(DelegationError):
        signer(settings)  # nothing here can mint a delegation
    assert not missing.exists()  # and no private key was created to make it work


def test_a_deployed_verifier_without_public_keys_refuses_to_start(tmp_path: Path) -> None:
    settings = get_settings().model_copy(
        update={
            "env": "staging",
            "delegation_private_key": None,
            "delegation_key_path": tmp_path / "absent.pem",
            "delegation_public_keys": [],
        }
    )
    with pytest.raises(DelegationError, match="public key"):
        verifier(settings, AUD)


def test_development_may_derive_the_public_key_from_the_local_key_file(tmp_path: Path) -> None:
    settings = get_settings().model_copy(
        update={
            "env": "development",
            "delegation_private_key": None,
            "delegation_key_path": tmp_path / "dev.pem",
            "delegation_public_keys": [],
        }
    )
    token = signer(settings).sign(
        tenant_id=uuid.uuid4(), principal_id=uuid.uuid4(), audience=AUD, tools=["get_invoice"]
    )
    assert verifier(settings, AUD).verify(token).audience == AUD


def test_old_and_new_keys_both_verify_while_they_overlap() -> None:
    old, new, stranger = (Ed25519PrivateKey.generate() for _ in range(3))
    both = DelegationVerifier([old.public_key(), new.public_key()], AUD)
    assert both.verify(_token(old)).audience == AUD
    assert both.verify(_token(new)).audience == AUD
    with pytest.raises(DelegationError, match="unknown delegation key"):
        both.verify(_token(stranger))
    after = DelegationVerifier([new.public_key()], AUD)  # the old key retired
    with pytest.raises(DelegationError, match="unknown delegation key"):
        after.verify(_token(old))


def test_a_delegation_without_a_key_id_needs_a_single_key() -> None:
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    now = int(time.time())
    claims = {
        "iss": DELEGATION_ISSUER,
        "aud": AUD,
        "sub": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "tools": ["get_invoice"],
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": uuid.uuid4().hex,
    }
    unnamed = jwt.encode(claims, key, algorithm="EdDSA")  # signed by an older signer
    assert DelegationVerifier(key.public_key(), AUD).verify(unnamed).audience == AUD
    with pytest.raises(DelegationError, match="names no key"):
        DelegationVerifier([key.public_key(), other.public_key()], AUD).verify(unnamed)
