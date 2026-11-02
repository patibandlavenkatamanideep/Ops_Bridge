"""OpenID Connect Back-Channel Logout 1.0: the identity provider tells OpsBridge that a
session there has ended, and OpsBridge ends the sessions it opened from it.

Without it, a person removed at the identity provider kept OpsBridge until their session
went idle or reached its absolute limit (30 minutes and 8 hours; production review A16).
The identity provider POSTs a signed logout token naming the person (`sub`), the session
(`sid`), or both. It is accepted only when it is signed by a key the provider publishes,
names this provider and this client, is current, carries the back-channel logout event and
no nonce (so an ID token cannot pass for one).

Keycloak 26 sends a logout token for each of a person's sessions when an administrator signs
them out, or when they sign out elsewhere. Disabling the account alone sends nothing and
ends no session, so removing someone's access is: disable the account, then sign out its
sessions (docs/operations.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt

BACKCHANNEL_EVENT = "http://schemas.openid.net/event/backchannel-logout"
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
MAX_TOKEN_CHARS = 8192
MAX_CLAIM_CHARS = 255
CLOCK_SKEW_SECONDS = 30
_TYPES = ("", "jwt", "logout+jwt", "application/logout+jwt")


class LogoutTokenInvalid(Exception):
    """The logout token is refused; the reason never includes the token."""


class UnknownSigningKey(LogoutTokenInvalid):
    """No published key matches the token: the provider may have rotated its keys."""


@dataclass(frozen=True)
class LogoutClaims:
    subject: str | None
    sid: str | None


def _signing_key(jwks: dict[str, Any], kid: Any) -> Any:
    try:
        keys = jwt.PyJWKSet.from_dict(jwks).keys
    except jwt.PyJWTError as exc:
        raise UnknownSigningKey("the provider publishes no usable key") from exc
    usable = [k for k in keys if k.public_key_use != "enc"]
    matching = [k for k in usable if k.key_id == kid] if kid is not None else usable
    if len(matching) != 1:
        raise UnknownSigningKey("no published key matches the token")
    return matching[0].key


def verify_logout_token(
    token: str, jwks: dict[str, Any], *, issuer: str, audience: str
) -> LogoutClaims:
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise LogoutTokenInvalid("missing or oversized logout token")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise LogoutTokenInvalid(f"not a signed token: {type(exc).__name__}") from exc
    alg = header.get("alg")
    if alg not in ALGORITHMS:
        raise LogoutTokenInvalid(f"algorithm {alg!r} is not accepted")
    if str(header.get("typ", "")).lower() not in _TYPES:
        raise LogoutTokenInvalid("not a logout token (typ)")
    key = _signing_key(jwks, header.get("kid"))
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=audience,
            issuer=issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["iss", "aud", "iat", "exp", "jti"]},
        )
    except jwt.PyJWTError as exc:
        raise LogoutTokenInvalid(f"logout token refused: {type(exc).__name__}") from exc
    events = claims.get("events")
    if not isinstance(events, dict) or not isinstance(events.get(BACKCHANNEL_EVENT), dict):
        raise LogoutTokenInvalid("not a back-channel logout event")
    if "nonce" in claims:
        raise LogoutTokenInvalid("a logout token carries no nonce")
    subject, sid = claims.get("sub"), claims.get("sid")
    for name, value in (("sub", subject), ("sid", sid)):
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > MAX_CLAIM_CHARS
        ):
            raise LogoutTokenInvalid(f"{name} is not a usable identifier")
    if subject is None and sid is None:
        raise LogoutTokenInvalid("names neither a person nor a session")
    return LogoutClaims(subject, sid)
