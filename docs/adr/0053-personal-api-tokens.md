# ADR-0053: a signed-in user gets one personal, read-only API token

Status: accepted (2026-11-01)

## Context

The `server-scan-api` Claude skill needs a bearer token. The three static `INVENTORY_API_TOKEN_*`
are shared secrets: a call made with one is recorded as "api-token-auditor", so the audit trail cannot
say which person acted, and a leak can only be fixed by rotating a token every user and machine caller
shares. Handing the admin token to admin users would also let a prompt change maintenance or reservations.

## Decision

A signed-in user (session cookie) creates their own token on the UI's `/skill` page.

- `ssk_` plus 32 random bytes; only its SHA-256 is stored (`api_tokens`, unique `token_hash`), with a
  12-character prefix to tell it apart. The plain token is returned once, by `POST /api/v1/auth/token`.
- **One per user**, keyed by username: creating another replaces it, and `DELETE /auth/token` revokes it.
  An admin (a session, or the static admin token for scripts) lists all (`GET /auth/tokens`) and revokes
  anyone's (`DELETE /auth/tokens/{username}`); both answer 404 with login off.
- **Read-only role cap**: a viewer's token is `VIEWER`; an auditor's and an admin's is `AUDITOR`. Never
  `ADMIN`, so the writes (maintenance, reservation, reclassify, recalculate) stay with a session.
- **Expiry** `INVENTORY_PERSONAL_TOKEN_TTL_DAYS` (default 90, at most 365), checked on use. Group
  membership is not re-checked: removing someone from AD does not stop their token before it expires, so an
  admin revokes it.
- **Attribution**: a request with a token acts as `Actor(type=TOKEN, id="token:<username>")`, which the UI
  shows as "<username> (personal token)", so every audited action names the person.
- `TOKEN_CREATED` and `TOKEN_REVOKED` are audit events (data: role, expiry, `by_admin`); a use is not.
- Only a session can manage tokens: a token calling `POST /auth/token` gets 403. With `auth.enabled`
  false the endpoints answer 404 and the page shows no token section.
- `get_current_actor` checks the three static tokens first, then `ssk_...` against the store (a Mongo
  failure counts as no match). A presented `ssk_` token that does not authenticate is a 401 even beside a
  valid session cookie; it is never rescued by the cookie. `last_used_at` is written at most
  every 5 minutes.
- The skill download names its token line for the role: `VIEWER_TOKEN` for a viewer, else `AUDITOR_TOKEN`.

## Consequences

Creating and revoking write the audit event first, so a failed write leaves the token untouched.
Revocation is immediate (no cache). A leaked token is one user's, with that user's name on what it did.
The collection holds no secret an attacker can replay. A user can have only one token, so two machines share it.
