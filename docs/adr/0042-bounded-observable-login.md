# ADR-0042: A bounded, observable login path

## Status

Accepted (2026-10-06)

## Context

On 2026-10-06 one login took ~25 s across three attempts (a 503 after 15,352 ms, then two slow
successes). Cause: a transient stall on the AD API, which normally answers in 32-52 ms. Three gaps
made it worse and hard to diagnose:

1. The 503's log line carried only `error_code`; the message ("Read timed out") was discarded, so
   the cause had to be inferred from the 15 s duration.
2. The AD API call had one 15 s timeout and no retry. httpx applies a float timeout to connect,
   read, write and pool *each*, not to the whole request.
3. ldap3 had no `connect_timeout` or `receive_timeout`. Verified in 2.9.1's sync strategy: with no
   `receive_timeout` the socket has no timeout and `recv()` blocks forever, so a hung directory would
   strand a worker thread for good. A fleet-wide p95 panel showed nothing (max 0.11 s) because a few slow
   logins are diluted by thousands of probes and polls.

## Decision

- **ldap3**: `Server(connect_timeout=...)`, `Connection(receive_timeout=...)`. `receive_timeout` is an
  `int` of whole seconds, because ldap3 passes it to `struct.pack('LL', ...)` (a float raises
  `struct.error`, which ldap3 does not catch). The bind runs on `run_abandonable` (a daemon thread,
  `app.infrastructure.blocking`), not `asyncio.to_thread`: an abandoned worker in any
  `ThreadPoolExecutor` blocks interpreter shutdown, and a dedicated executor does not help. The socket
  timeouts are what end the call; `asyncio.timeout` only abandons the await. `get_info=NONE`: a bind
  needs no rootDSE/schema, which `ALL` downloaded on every login.
- **AD API**: `httpx.Timeout(connect=3, read=4, write=4, pool=3)` and one retry with 100-200 ms of
  jitter, only on a timeout, a connection error or 502/503/504 (a GET, so safe to repeat), never on a 4xx
  or an invalid body. A wrong-password LDAP bind is never retried (AD lockout counts it).
- **Deadline**: `auth_login_deadline_seconds` (10) wraps the whole attempt in `asyncio.timeout`, so the
  per-call timeouts and retries can never add up to a hung login.
- **Group cache**: each group's recursive member list is cached in Redis (`CachedGroupMembership`,
  `auth_group_cache_ttl_seconds`, 120; 0 disables), keyed per group so every user shares one lookup.
  A failed AD API call is never cached and never answered from stale data: a demoted admin must not
  keep access through an outage. A role change takes up to the TTL to apply; Redis errors are a miss.
- **Observability**: `ServiceUnavailableError` carries `dependency` and `reason` (log only, not in the
  response body); the handler logs them with the message at WARNING for every 5xx `AppError`. One
  `auth.login` line per attempt (outcome, duration), `ldap.bind` / `ad_api.*` lines with durations,
  `auth.role_resolved` (which list or group decided), `auth.rejected` (a bearer mismatch or an invalid
  session), and the metrics `dependency_call_duration_seconds{dependency,outcome}` and
  `auth_logins_total{outcome}`. The same timing and outcome logs cover `GET /servers/available`'s live
  recheck (`available.recheck`, `available.filled`), the other call to an outside system.
- **Deployed with the metrics** (`deploy/helm/server-scan/templates/backend-prometheusrule.yaml`,
  `deploy/grafana/server-scan-dashboard.json`): a `server-scan.auth.rules` group (login outcome
  rate, login-route p95/p99, per-dependency rate, p95/p99 and failure ratio, summed across replicas),
  alerts `ServerScanLoginDependencyFailing`, `ServerScanLoginSlow` and
  `ServerScanAvailableRecheckFailing` (thresholds `metrics.prometheusRule.loginDependencyFailureRatio`
  and `loginSlowSeconds`, read with `default` because the Deploy job renders against redbull-platform's
  older values), and a "Login and external calls" dashboard row.
- Never logged: the password, the bearer token or the session cookie. Messages are built from the
  exception type and the library's own message, not from request bodies or headers.

## Consequences

- `INVENTORY_AUTH_REQUEST_TIMEOUT_SECONDS` drops from 15 to 4 and now means the AD API read timeout. An
  install that set it explicitly keeps its value; the Helm chart never set it, so it gets the new default.
- A stall of the AD API now costs about 4 s plus one retry, not 15 s plus a user retry. A hung LDAP fails
  at `receive_timeout` instead of never.
- Not done, on purpose: a circuit breaker (at 2.5-5k servers and a handful of logins a minute it adds
  state for little), hedged requests, and serving stale roles on error.
- The per-attempt `ad_api.attempt_failed` and `ldap.bind_failed` lines are the ones to alert on or grep.
