# ADR-0039: Per-username login throttle in Redis, failing open

## Status

Accepted (2026-10-04)

## Context

`POST /auth/login` had no throttling of its own (ADR-0034, "Deferred"); it relied on Active Directory's account lockout. That reliance has a cost beyond brute force: every failed attempt is a real LDAP bind against the user's AD account, so someone guessing at one username locks that person out of everything else in the domain before this app ever refuses.

## Decision

1. **Count failures per account, not per source IP** (OWASP Authentication Cheat Sheet: the counter belongs to the account so rotating IPs does not help). The key is the submitted username, trimmed and lower-cased, hashed (`si:1:loginfail:<sha256[:32]>`), and it applies whether or not the username exists, so the response never reveals which accounts are real.
2. **Lock before binding.** After `INVENTORY_LOGIN_MAX_FAILURES` (default 5) failures within `INVENTORY_LOGIN_LOCKOUT_SECONDS` (default 900) the endpoint answers `429 RATE_LIMITED` with `Retry-After`, without contacting LDAP. Keep the limit below AD's own threshold. NIST SP 800-63B only requires an upper bound of 100 consecutive failures, so 5 is well inside it. 0 disables the throttle.
3. **Only wrong credentials count.** A valid login with no role (403), an AD outage (503) and a success do not add to the counter; a successful or role-less valid login clears it.
4. **One atomic step.** `INCR` plus `EXPIRE ... NX` in a `MULTI` pipeline, so the window starts at the first failure and later failures do not extend it. The key always has a TTL, so no state can become a permanent lock. (`EXPIRE NX` needs Redis 7; the project runs Redis 8.)
5. **Fail open.** Redis is a cache in this platform. Any Redis error, or a client that never connected, lets the login proceed and logs `login_throttle.redis_unavailable`; AD's lockout is the backstop. A login can wait up to the Redis socket timeout (2 s) per call when Redis is black-holed; a closed port or unresolvable host fails in milliseconds.

`RateLimitedError` (429) gained `retry_after_seconds`, which the problem handler sends as `Retry-After`. The login page already shows the server's `detail` for statuses it does not map.

## Consequences

- A user can be locked out of this app for 15 minutes by someone failing logins as them (OWASP's lockout-DoS caveat). Accepted: the alternative is that AD locks them out of everything, and the window is short and automatic.
- **Not covered, and why:** a per-IP limit. Behind the OpenShift Route `request.client` is the proxy, so a per-IP counter would throttle everyone together; trusting `X-Forwarded-For` is a separate decision. Password spraying (one password, many usernames) is therefore still left to AD's own protections.
- No session revocation remains an accepted trade-off (ADR-0034).
- Config: `login_max_failures`, `login_lockout_seconds` (`.env.example`; Helm `auth.loginMaxFailures`, `auth.loginLockoutSeconds`, rendered only when present so an older downstream `values.yaml` still renders).
- Tests: `tests/unit/infrastructure/redis/test_login_throttle.py` (lockout, case-insensitivity, reset, window not extended, disabled, fail-open) and `TestLoginThrottle` in `tests/api/test_auth.py`, both on an in-memory Redis fake (`tests/fake_redis.py`).
