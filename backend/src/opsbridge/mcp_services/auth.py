"""Authorization for the MCP services: service identity + delegated human context.

Two independent credentials are required on every call:

1. `Authorization: Bearer <service token>` — an OAuth access token from the identity
   provider (client credentials), audience-restricted to this MCP resource. It proves
   WHICH SERVICE is calling (e.g. `opsbridge-worker`). Verified with the provider's JWKS.
2. `X-OpsBridge-Delegation: <delegation token>` — a short-lived JWT signed by the OpsBridge
   backend (Ed25519) naming the tenant, the human principal on whose behalf the call is
   made, the case/run/operation scope and the tools allowed. It never contains the user's
   own access token; user tokens are not forwarded to other services.

Both are standard JWTs verified with PyJWT; no custom cryptography. The MCP service then
re-checks the human principal's CURRENT membership and permissions in the database, so a
broadly credentialed service cannot exceed the principal's business authority.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from mcp.server.auth.provider import AccessToken

DELEGATION_HEADER = "x-opsbridge-delegation"
DELEGATION_ISSUER = "opsbridge-backend"
MAX_DELEGATION_SECONDS = 300
# How far an issuer's clock may run ahead of this service's (sign-in allows the same). A
# token used the moment it is issued can carry an issue time just after this clock's reading;
# PyJWT allows no difference unless given `leeway`, which would also stretch expiry, so its
# not-yet-valid checks are replaced by `check_issued` and expiry stays as issued.
CLOCK_SKEW_SECONDS = 30


class DelegationError(Exception):
    pass


def check_issued(claims: dict[str, Any]) -> None:
    """Refuse a token issued, or valid from, later than the allowed clock difference."""
    limit = time.time() + CLOCK_SKEW_SECONDS
    for name in ("iat", "nbf"):
        if name not in claims:
            continue
        value = claims[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise jwt.InvalidTokenError(f"{name} is not a time")
        if value > limit:
            raise jwt.ImmatureSignatureError(f"the token is not valid yet ({name})")


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    """Development helper: create the delegation signing key if absent (0600)."""
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise DelegationError("delegation key is not Ed25519")
        return key
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return key


@dataclass(frozen=True)
class Delegation:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    audience: str
    tools: frozenset[str]
    case_id: uuid.UUID | None
    run_id: uuid.UUID | None
    operation_id: uuid.UUID | None
    jti: str
    expires_at: int


@dataclass
class DelegationSigner:
    private_key: Ed25519PrivateKey

    def sign(
        self,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        audience: str,
        tools: list[str],
        case_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
        ttl_seconds: int = 120,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": DELEGATION_ISSUER,
            "aud": audience,
            "sub": str(principal_id),
            "tenant_id": str(tenant_id),
            "tools": sorted(tools),
            "iat": now,
            "nbf": now,
            "exp": now + min(ttl_seconds, MAX_DELEGATION_SECONDS),
            "jti": uuid.uuid4().hex,
        }
        for k, v in (("case_id", case_id), ("run_id", run_id), ("operation_id", operation_id)):
            if v is not None:
                claims[k] = str(v)
        return jwt.encode(claims, self.private_key, algorithm="EdDSA")

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()


@dataclass
class DelegationVerifier:
    public_key: Ed25519PublicKey
    audience: str

    def verify(self, token: str | None) -> Delegation:
        if not token:
            raise DelegationError("missing delegation token")
        try:
            c = jwt.decode(
                token,
                self.public_key,
                algorithms=["EdDSA"],
                audience=self.audience,
                issuer=DELEGATION_ISSUER,
                options={
                    "require": ["exp", "iat", "sub", "aud", "iss", "jti"],
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            check_issued(c)
        except jwt.PyJWTError as exc:
            raise DelegationError(f"invalid delegation token: {type(exc).__name__}") from exc
        if c["exp"] - c["iat"] > MAX_DELEGATION_SECONDS:
            raise DelegationError("delegation lifetime exceeds the maximum")
        try:
            return Delegation(
                tenant_id=uuid.UUID(c["tenant_id"]),
                principal_id=uuid.UUID(c["sub"]),
                audience=c["aud"],
                tools=frozenset(c.get("tools", [])),
                case_id=uuid.UUID(c["case_id"]) if c.get("case_id") else None,
                run_id=uuid.UUID(c["run_id"]) if c.get("run_id") else None,
                operation_id=uuid.UUID(c["operation_id"]) if c.get("operation_id") else None,
                jti=c["jti"],
                expires_at=int(c["exp"]),
            )
        except (KeyError, ValueError) as exc:
            raise DelegationError("malformed delegation claims") from exc


@dataclass
class JwksTokenVerifier:
    """Verifies identity-provider access tokens for one MCP resource (audience)."""

    issuer: str
    audience: str
    allowed_clients: frozenset[str]
    jwks_url: str | None = None
    _jwks: Any = field(default=None, init=False, repr=False)

    def _client(self) -> Any:
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(
                self.jwks_url or f"{self.issuer}/protocol/openid-connect/certs",
                cache_keys=True,
                lifespan=300,
            )
        return self._jwks

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self._client().get_signing_key_from_jwt(token)
            c = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256", "EdDSA"],
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud"],
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            check_issued(c)
        except (jwt.PyJWTError, OSError):
            return None
        client = c.get("azp") or c.get("client_id")
        if client not in self.allowed_clients:
            return None
        return AccessToken(
            token=token,
            client_id=str(client),
            scopes=str(c.get("scope", "")).split(),
            expires_at=int(c["exp"]),
            resource=self.audience,
            subject=c.get("sub"),
            claims={"iss": c["iss"]},
        )
