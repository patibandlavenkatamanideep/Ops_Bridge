# Account lifecycle: removing a person

What happens to a person's access, work and data when they leave a tenant or the
organization, as OpsBridge implements it on 5 November 2026. Each item names its code or test.
The decisions the owner must make before a pilot are listed at the end (production review
A16). Nothing here has been exercised with a real identity provider or a real departure.

## Access

1. **Disable the account at the identity provider, then sign the person out there.**
   - Disabling stops new sign-ins.
   - It does not by itself end a session OpsBridge already holds: sessions are checked locally
     until they go idle (`OPSBRIDGE_SESSION_IDLE_MINUTES`, 30) or reach their limit
     (`OPSBRIDGE_SESSION_ABSOLUTE_HOURS`, 8).
   - Signing the person out at the provider sends a back-channel logout. OpsBridge then ends
     the sessions it names at once (`/auth/backchannel-logout`, `ops.revoke_idp_sessions`,
     `tests/integration/test_backchannel_logout.py`).
   - Whether disabling a user also signs them out depends on the provider and its
     configuration. Check it on the provider in use.
2. **Revoke the membership in OpsBridge** (Tenant settings → Members, `tenant.manage_members`).
   - Every request resolves the person's membership server-side and requires it to be active
     (`api/deps.py`), so a revoked member loses access to that tenant on their next request,
     whatever their session.
   - Revocation is per tenant: do it in each tenant the person belonged to.
   - Reinstating restores access
     (`test_a_revoked_member_loses_access_and_reinstating_restores_it`).
3. **Bounded delay.** With both steps, access ends at the next request. With only the
   identity-provider step and no back-channel logout, it ends within the idle limit, or at
   worst the absolute limit.
4. **Fallback when the provider's logout did not reach OpsBridge** (Keycloak does not retry
   one): `opsbridge end-sessions --subject <the person's id at the provider>` ends their live
   sessions now, as a back-channel logout naming only them would (docs/operations.md). Revoking
   the membership (step 2) ends their access in that tenant either way. Ended sessions are
   pruned 24 hours later with the ID tokens they hold (`ops.prune_sessions`).

## Work in progress

- **Approvals.** An approval by a person who is no longer an active approver, or whose
  approval scope no longer covers the action, is refused at dispatch if nothing can have been
  sent (`approver_still_authorized`,
  `test_revoking_an_approver_stops_their_earlier_approval_before_dispatch`). A write that may
  already have landed is still recovered and kept as evidence, whatever happened to the
  approver since (`test_a_landed_write_is_recovered_after_the_approver_is_revoked`).
- **Proposals awaiting their decision.** Proposals remain and another approver can decide
  them. Separation of duties is checked against the decider, as before.
- **Cases they own.** Cases keep their owner. The queue marks a case whose owner is no longer
  an active investigator (`owner_active`), and an investigator or administrator takes or
  assigns it, with the owner they saw checked against the current one (`services/cases.py`).
  Nothing reassigns cases automatically.
- **Invitations they sent.** These lapse when the inviter loses the authority to invite
  (`test_an_invitation_lapses_when_its_inviter_loses_authority`).

## Data

- **The profile** (`users`: name, e-mail, the provider's subject) is kept while the account
  exists. Nothing deletes or anonymizes it. Cases, approvals and audit events refer to it by id.
- **Sessions and their ID tokens** are deleted 24 hours after they end.
- **Audit events, approvals, operations and attempts** are kept. They are the record of who
  did what.
- **Messages they wrote** follow the tenant's `conversation_days` retention, which clears the
  text and keeps the row.
- **Backups and object versions** still hold whatever they held when taken. Deleting a person's
  data does not reach a backup until it expires, under a backup retention period not set yet,
  nor a noncurrent object version until the bucket's lifecycle rule removes it.

## Decisions for the owner (before a pilot)

1. Whether disabling a user at the identity provider must also sign them out there, and who
   does it.
2. How soon a departed person's access must end: is the idle limit acceptable when the
   provider sends no logout?
3. Who takes over a departed investigator's cases and pending proposals, and within what time.
4. How long a profile is kept after departure. Whether it is deleted, or its name and e-mail
   replaced while the id stays for the audit trail.
5. Whether a legal hold can stop retention for a person or case. None exists.
6. How long backups and object versions are kept, which bounds how long deleted data survives.
