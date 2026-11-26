# ADR-0072: A blocked-users list that beats every role list and group

**Status:** Accepted, implemented 2026-11-26.

## Context

Roles come from six lists (ADR-0034, ADR-0043): admin/auditor/viewer users and groups. A user who is in a viewer
group (often a large AD group the operator does not own) could not be kept out without changing AD. The operator
asked for a list of usernames to refuse even when a list or group admits them.

## Decision

- **`INVENTORY_BLOCKED_USERS`** (Helm `auth.blockedUsers: "user1,user2"`), comma-separated, case-insensitive,
  live-mounted like the other lists (`INVENTORY_BLOCKED_USERS_FILE`), so an edit applies without a pod restart.
- **Checked after the LDAP bind, before every list:** a wrong password stays 401 "wrong password", so the list
  never tells a caller that a username exists or is blocked; a right password answers 403 "Your account is blocked
  from this app" and records `LOGIN_DENIED` with `data.role` `BLOCKED` (`LoginResult.BLOCKED`), never a session.
- **Checked on every request too:** `get_current_actor` refuses a blocked user's session cookie and personal API
  token (`token:<user>`) with 401, and `/auth/me` reports them logged out. The session is stateless (ADR-0034), so
  without this a user blocked mid-session would keep access until the cookie expired (8 h).
- The static bearer tokens (`api-token-admin|viewer|auditor`) are not usernames and are not affected.

## Consequences

- Blocking is immediate and needs no AD change; unblocking is removing the name.
- One small file read per authenticated request when the live file is mounted.
- An older API image ignores the setting and decodes a `LOGIN_DENIED` event with role `BLOCKED` fine (`data` is free-form).
