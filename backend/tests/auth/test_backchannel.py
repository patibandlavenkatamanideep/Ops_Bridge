"""Which logout tokens end sessions (OpenID Connect Back-Channel Logout 1.0, production
review A16). The accepted token is shaped like the ones Keycloak 26.8 sent the local stack:
RS256, `typ: logout+jwt`, with `iss`, `aud`, `sub`, `sid`, `jti`, `iat`, `exp` and the
back-channel logout event."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from opsbridge.auth.backchannel import (
    BACKCHANNEL_EVENT,
    LogoutClaims,
    LogoutTokenInvalid,
    UnknownSigningKey,
    verify_logout_token,
)

ISSUER = "http://localhost:8081/realms/opsbridge"
CLIENT = "opsbridge-web"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(*keys: tuple[str, Any]) -> dict[str, Any]:
    out = []
    for kid, key in keys:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
    # An encryption key, as Keycloak also publishes; never used to verify.
    enc = jwt.algorithms.RSAAlgorithm.to_jwk(OTHER.public_key(), as_dict=True)
    out.append({**enc, "kid": "enc-1", "use": "enc", "alg": "RSA-OAEP"})
    return {"keys": out}


PUBLISHED = jwks(("sig-1", KEY))


def token(
    *, key: Any = KEY, kid: str | None = "sig-1", headers: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (), **claims: Any,
) -> str:  # fmt: skip
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "a1b2c3",
        "sid": "kc-session-1",
        "jti": "jti-1",
        "iat": now,
        "exp": now + 120,
        "typ": "Logout",
        "events": {BACKCHANNEL_EVENT: {}},
        **claims,
    }
    for name in drop:
        body.pop(name)
    head = {"typ": "logout+jwt", **({"kid": kid} if kid else {}), **(headers or {})}
    return jwt.encode(body, key, algorithm="RS256", headers=head)


def verify(raw: str, keys: dict[str, Any] = PUBLISHED) -> LogoutClaims:
    return verify_logout_token(raw, keys, issuer=ISSUER, audience=CLIENT)


def test_a_logout_token_as_keycloak_sends_it_is_accepted() -> None:
    assert verify(token()) == LogoutClaims("a1b2c3", "kc-session-1")


@pytest.mark.parametrize(
    ("drop", "expected"),
    [(("sid",), LogoutClaims("a1b2c3", None)), (("sub",), LogoutClaims(None, "kc-session-1"))],
)
def test_a_token_may_name_only_the_person_or_only_the_session(
    drop: tuple[str, ...], expected: LogoutClaims
) -> None:
    assert verify(token(drop=drop)) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (lambda: token(key=OTHER), "InvalidSignatureError"),
        (lambda: token(iss="http://idp.example/realms/other"), "InvalidIssuerError"),
        (lambda: token(aud="another-client"), "InvalidAudienceError"),
        (lambda: token(exp=int(time.time()) - 120), "ExpiredSignatureError"),
        (lambda: token(iat=int(time.time()) + 600), "ImmatureSignatureError"),
        (lambda: token(drop=("jti",)), "MissingRequiredClaimError"),
        (lambda: token(drop=("exp",)), "MissingRequiredClaimError"),
        (lambda: token(drop=("events",)), "not a back-channel logout event"),
        (lambda: token(events={"http://schemas.openid.net/event/other": {}}), "not a back-channel"),
        (lambda: token(nonce="n-1"), "carries no nonce"),
        (lambda: token(drop=("sub", "sid")), "names neither"),
        (lambda: token(sub=12345), "InvalidSubjectError"),  # PyJWT checks sub's type itself
        (lambda: token(sub=""), "sub is not a usable identifier"),
        (lambda: token(sid="x" * 300), "sid is not a usable identifier"),
        (lambda: token(headers={"typ": "at+jwt"}), "not a logout token"),
        (lambda: "not.a.token", "not a signed token"),
        (lambda: "x" * 9000, "oversized"),
    ],
)
def test_a_token_that_is_not_a_current_logout_from_this_provider_is_refused(
    raw: Any, reason: str
) -> None:
    with pytest.raises(LogoutTokenInvalid, match=reason):
        verify(raw())


def test_an_id_token_from_the_same_provider_cannot_pass_for_a_logout_token() -> None:
    now = int(time.time())
    id_token = jwt.encode(
        {"iss": ISSUER, "aud": CLIENT, "sub": "a1b2c3", "sid": "kc-session-1", "jti": "j",
         "iat": now, "exp": now + 300, "nonce": "n-1", "typ": "ID"},
        KEY, algorithm="RS256", headers={"kid": "sig-1", "typ": "JWT"},
    )  # fmt: skip
    with pytest.raises(LogoutTokenInvalid, match="not a back-channel logout event"):
        verify(id_token)


@pytest.mark.parametrize("alg", ["none", "HS256"])
def test_unsigned_or_shared_secret_tokens_are_refused(alg: str) -> None:
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": CLIENT, "sub": "a1b2c3", "events": {BACKCHANNEL_EVENT: {}}},
        "shared-secret-value-of-sufficient-length" if alg == "HS256" else None,
        algorithm=alg,
    )
    with pytest.raises(LogoutTokenInvalid, match="is not accepted"):
        verify(unsigned)


def test_a_token_signed_by_a_key_not_yet_fetched_asks_for_the_keys_again() -> None:
    rotated = token(key=OTHER, kid="sig-2")
    with pytest.raises(UnknownSigningKey):
        verify(rotated)
    assert verify(rotated, jwks(("sig-1", KEY), ("sig-2", OTHER))).sid == "kc-session-1"
