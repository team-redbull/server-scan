# ADR-0043: An auditor role, and the audit trail is not for viewers

Date: 2026-10-07
Status: Accepted (extends ADR-0034)

## Context

ADR-0034 gave the platform two roles, admin and viewer, and every read endpoint needed only *a* resolved
caller. That put the audit trail (who put which server into maintenance and why, who reclassified what,
usernames and free-text reasons) in front of every viewer, through the Events page, a server's History tab
and the `/events` endpoints. The operator asked on 2026-10-07 for that to be admin-only in the UI, and for
a separate read-only identity that *can* read it: the `server-scan-api` skill answers "what changed on
srv-12" from the events endpoints, and should not hold an admin token to do it.

## Decision

- **Three roles.** `ADMIN` (everything, including writes), `AUDITOR` (read-only plus the audit trail) and
  `VIEWER` (read-only, no audit trail). `Role.AUDITOR` is new and additive.
- **The audit trail is enforced in the API, not only hidden.** `events_router` is mounted with
  `Depends(require_audit_access)`: `/events`, `/events/actors` and `/servers/{id}/events` answer `403
  FORBIDDEN` to a viewer (session or token) and `401` to nobody. Hiding the nav link and the tab alone would
  have left the data one `curl` away.
- **Where an auditor comes from.** Both ways, like the other roles: `auth.auditorGroups` /
  `auth.auditorUsers` (`INVENTORY_AUDITOR_GROUPS` / `_USERS`, with the same live-mounted `_FILE` variants, so
  an edit applies on the next login with no pod restart) and a static `auth.apiTokens.auditor`
  (`INVENTORY_API_TOKEN_AUDITOR`) for the skill.
- **Order of precedence: admin, then auditor, then viewer**, a user's own list before its groups. A user in
  several lists gets the most privileged role, as before. `require_admin` is unchanged, so an auditor cannot
  write.
- **The three API tokens must differ.** `Settings` refuses to start if two are equal, because the first one
  checked (admin) would otherwise silently win for both holders.
- **Frontend:** `useCanReadAudit()` (admin or auditor) hides the Events nav link, replaces the `/events` route
  with a "not permitted" message (`AuditGate`) and drops the History tab. A viewer sees *nothing* of it, unlike
  the maintenance switch, which is shown disabled (ADR-0034, decision 6): the existence of the tab tells a
  viewer nothing useful. `MaintenanceToggle` treats `AUDITOR` as non-admin.
- **Rules & Policies and Architecture stay open to every role**, by the operator's decision.

## Consequences

- An existing install changes behaviour on upgrade: every viewer loses the Events page and the History tab,
  and a viewer token loses `/events`. Nothing to configure to get that; to give someone the trail, add them to
  `auth.auditorGroups` / `auditorUsers` or hand the skill the new auditor token.
- The `server-scan-api` skill now uses `AUDITOR_TOKEN` (was `VIEW_TOKEN`). A deployment that keeps a viewer
  token in it still works for everything but events.
- No stored data changes. `Actor.role` is stored on audit events, but only writes create events and writes
  are admin-only, so `AUDITOR` is never written; an old reader never meets it.
- The Helm templates read the new keys with `default ""`, so redbull-platform's older `values.yaml` still
  renders.
- Not done: finer-grained permissions (per site, per event type). Three roles cover the stated need.
