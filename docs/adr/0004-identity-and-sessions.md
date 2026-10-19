# ADR 0004: OIDC login with server-side sessions

Status: accepted (2026-10-14)

## Decision
- Authorization Code flow with PKCE (S256) against Keycloak, implemented with Authlib's
  Starlette client, which validates state, nonce, issuer, audience, signature and expiry
  of the ID token.
- Transient login state (state, nonce, PKCE verifier) is held in a short-lived signed
  cookie by Starlette's SessionMiddleware, used only during the login redirect.
- After login the server creates a row in `user_sessions` and sets an opaque, random,
  HttpOnly `opsbridge_session` cookie (SameSite=Lax; Secure outside local development).
  The ID token is stored server-side for RP-initiated logout; access tokens are not kept
  because OpsBridge does not call APIs with the user's token.
- Sessions have an absolute lifetime (default 8 h) and idle timeout (default 30 min).
- Cookie-authenticated writes require an `X-CSRF-Token` header equal to the session's CSRF
  token (synchronizer token). CORS is restricted to the configured UI origin.
- Users and tenant memberships are stored server-side. Identity-provider group claims
  are **not** used to grant tenant access; membership rows are.
- Machine identities (`worker`, `mcp` callers) use OAuth client credentials from
  Keycloak, audience-restricted, and are recorded separately from the human principal.

## Consequences
- Integration tests drive the real Keycloak login form over HTTP.
- Unit tests create sessions through a test fixture that writes a session row directly;
  there is no HTTP backdoor endpoint.
- Local demo passwords are development-only and rejected when `OPSBRIDGE_ENV=production`.
