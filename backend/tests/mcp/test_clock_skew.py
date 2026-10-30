"""Tokens from an issuer whose clock runs a little ahead of the MCP service's.

The identity provider runs in another container or host. A token used the moment it is
issued can carry an issue time a few milliseconds after the service's clock reads (2 in 200
fresh worker tokens locally, up to 8 ms ahead); refusing those failed MCP calls at random
with a 401, which the worker can only record as an outcome it does not know. A token from
further ahead is still refused, and expiry is not stretched to make room.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from opsbridge.mcp_services.auth import (
    CLOCK_SKEW_SECONDS,
    DELEGATION_ISSUER,
    DelegationError,
    DelegationVerifier,
    JwksTokenVerifier,
)

ISSUER = "https://idp.example/realms/opsbridge"
AUDIENCE = "opsbridge-casework-mcp"
WORKER = "opsbridge-worker"
KEY = Ed25519PrivateKey.generate()


class _ProviderKeys:
    """Stands in for the identity provider's JWKS endpoint."""

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return SimpleNamespace(key=KEY.public_key())


def _service_token_accepted(times: dict[str, Any]) -> bool:
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "azp": WORKER, **times}, KEY, algorithm="EdDSA"
    )
    verifier = JwksTokenVerifier(ISSUER, AUDIENCE, frozenset({WORKER}))
    verifier._jwks = _ProviderKeys()
    return asyncio.run(verifier.verify_token(token)) is not None


def _delegation(times: dict[str, Any]) -> str:
    claims = {
        "iss": DELEGATION_ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "tools": ["get_follow_up_task"],
        "jti": uuid.uuid4().hex,
        **times,
    }
    return jwt.encode(claims, KEY, algorithm="EdDSA")


def _delegation_accepted(times: dict[str, Any]) -> bool:
    try:
        DelegationVerifier(KEY.public_key(), AUDIENCE).verify(_delegation(times))
    except DelegationError:
        return False
    return True


@pytest.mark.parametrize("ahead", [1, CLOCK_SKEW_SECONDS - 5])
def test_a_token_issued_by_a_clock_slightly_ahead_is_accepted(ahead: int) -> None:
    issued = int(time.time()) + ahead
    times = {"iat": issued, "nbf": issued, "exp": issued + 120}
    assert _service_token_accepted(times)
    assert _delegation_accepted(times)


@pytest.mark.parametrize(
    "times",
    [
        # issued, or valid from, further ahead than any clock difference allows
        {"iat": "later", "exp": "later+120"},
        {"iat": "now", "nbf": "later", "exp": "later+120"},
        # expired a second ago: refused as issued, not given the skew allowance
        {"iat": "now-60", "exp": "now-1"},
        # times that are not times
        {"iat": "text", "exp": "now+120"},
        {"iat": "nan", "exp": "now+120"},
    ],
    ids=["issued_later", "valid_later", "expired", "iat_text", "iat_nan"],
)
def test_a_token_from_further_ahead_expired_or_malformed_is_refused(
    times: dict[str, str],
) -> None:
    now = int(time.time())
    later = now + CLOCK_SKEW_SECONDS + 60
    values: dict[str, Any] = {
        "now": now,
        "later": later,
        "later+120": later + 120,
        "now-60": now - 60,
        "now-1": now - 1,
        "now+120": now + 120,
        "text": "soon",
        "nan": float("nan"),
    }
    resolved = {claim: values[v] for claim, v in times.items()}
    assert not _service_token_accepted(resolved)
    assert not _delegation_accepted(resolved)
