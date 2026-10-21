# Local identity provider (development only)

`realm-opsbridge.json` is imported by the Keycloak container on start
(`start-dev --import-realm`). Secrets are `${ENV}` placeholders resolved by Keycloak
from the container environment (populated from `.env`); no secret is committed.

All users are synthetic, use `@example.test` addresses, and share the development-only
password `OPSBRIDGE_DEMO_USER_PASSWORD` from `.env`. Production configuration refuses
to start with `OPSBRIDGE_ENV=production` and this realm's issuer.

User IDs are fixed so `make seed-demo` can attach OpsBridge tenant memberships to the
OIDC subject (`iss` + `sub`). Tenant membership is stored in OpsBridge, not in Keycloak.

| Username | Tenant membership (seeded by OpsBridge) |
|---|---|
| furn-requester | demo-furniture: requester |
| furn-investigator | demo-furniture: investigator |
| furn-approver | demo-furniture: approver (all furniture action types) |
| furn-approver2 | demo-furniture: approver (record_review_note only) |
| furn-admin | demo-furniture: tenant_admin |
| furn-auditor | demo-furniture: auditor |
| it-requester | demo-it: requester |
| it-investigator | demo-it: investigator |
| it-approver | demo-it: approver |
| multi-member | demo-furniture: investigator + approver; demo-it: requester |
| new-hire | none: joins through an invitation (email verified by Keycloak) |
| unverified-hire | none: email **not** verified, so invitations are refused (negative test) |

Clients: `opsbridge-web` (confidential, authorization code + PKCE S256),
`opsbridge-worker` (client credentials; audiences `opsbridge-records-mcp` and
`opsbridge-casework-mcp`), and the two bearer-only MCP resource clients.

Keycloak imports this file only when the realm does not exist yet. After users are added
here, `make identity-sync` adds the missing ones to a running local Keycloak (Keycloak's
partial import with skip-if-exists; existing users and passwords are left alone).
