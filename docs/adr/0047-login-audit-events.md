# ADR-0047: Every authenticated login is an audit event, refused ones included

Date: 2026-10-12
Status: Accepted

Extends ADR-0034 (AD login, roles) and ADR-0043 (the audit trail is admin/auditor only).

## Context

The audit trail recorded what people *did* (maintenance, reservations) but not who got in. A user with a
valid AD account but no admin/auditor/viewer group was refused (403) and left only a structlog line and a
metric. The operator asked to see every login, whatever its outcome, with the role it resolved to.

## Decision

`POST /auth/login` records one event per authenticated login, best-effort, after the role is resolved:

| Event | When | `actor.role` | `data` |
|---|---|---|---|
| `LOGIN_SUCCEEDED` | credentials valid, role resolved | `ADMIN` / `AUDITOR` / `VIEWER` | `{"role": "<role>"}` |
| `LOGIN_DENIED` | credentials valid, listed in no admin/auditor/viewer list (the 403) | `null` | `{"role": "NO_PERMISSION"}` |

The actor is the normalised username (`USER`). The events carry no server, so they appear on the Events
page (filter "LOGIN_SUCCEEDED" / "LOGIN_DENIED") and not in a server's History tab; like the rest of the
trail they are visible to admin and auditor only and are removed by the retention job after
`retentionDays`.

- **A wrong password is not recorded.** The endpoint is unauthenticated: an event per bad attempt would let
  anyone grow the collection (spraying usernames also bypasses the per-username throttle). Those attempts stay
  visible as the `auth.login` log line (`outcome=wrong_password`) and the `auth_logins_total` counter. A
  `LOGIN_DENIED` needs a valid AD password, so it cannot be sprayed.
- **The write never blocks or fails a login.** It is capped at 2 s and any error is logged
  (`auth.login_audit_failed`) and swallowed: the login path must not depend on MongoDB (ADR-0042).
- **Static API tokens are not logins** and write nothing here; with `auth.enabled=false` there is no login.
- **No client IP.** `request.client` behind the Route is the proxy (ADR-0039), so an address would mislead.
- The per-user Grafana gauges (ADR-0029, capped at 200 actors) now count login events too.

## Consequences

- An operator can answer "who tried to get in without permission" with `GET /events?event_type=LOGIN_DENIED`.
- A busy fleet of users adds one small document per login; at the default 180-day retention that is bounded.
