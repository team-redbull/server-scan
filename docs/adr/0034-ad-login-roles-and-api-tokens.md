# ADR-0034: AD login with admin/viewer roles, a stateless session cookie, and two API tokens

Date: 2026-09-22
Status: Accepted

## Context

Every endpoint has been open since the platform's first slice
(`app.dependencies.get_current_actor` returns a fixed `unauthenticated`
actor — CLAUDE.md convention 6). Real auth was deliberately the very last
slice, deferred more than once at the operator's own request. On
2026-09-22 the operator asked for it directly: three roles — **admin**
(views everything, can put a server into and out of maintenance),
**viewer** (views everything, cannot touch maintenance), and **no
permission** (authenticated, but rejected outright) — resolved from the
operator's own air-gapped Active Directory via a REST "AD API" they
already run, whose contract was given as a full spec:

- Credentials are checked first, by an LDAP simple bind as
  `DOMAIN\sAMAccountName` — the bind succeeding *is* the credential check,
  no service account, no search.
- Group membership is recursive, via `GET {AD_API_URL}/groups/members/user
  ?samAccountName={GROUP}&isRecursive=true` with a `ClientId` header.
- A directory failure (LDAP down, the AD API down or erroring) must never
  be reported as a wrong password.

The operator generalized the spec's single admin/login group pair to
**four independent lists** — `admin_groups`, `view_groups`, `admin_users`,
`viewer_users` — with admin checked before viewer, and asked for a helm/
env flag to bypass login entirely in an environment with no AD reachable
(this repo's own dev/test environment, and the default for every existing
install). A later message added two static API tokens, one admin and one
viewer, so `GET /api/v1/servers/available`'s machine caller (the BMH
generator, ADR-0032) can keep working with no AD login at all.

## Decision

### 1. Role model

`Role` (`app.domain.models.audit_event`) is `ADMIN | VIEWER` — a stored,
optional field on `Actor`, so an audit event now records who really did
something. `NO_PERMISSION` is not a `Role`: a login that resolves to it is
rejected at `POST /auth/login` (403) before any session or `Actor` exists,
matching the spec's own return values (`"admin" | "login" | "no_permission"
| False`, renamed here to fit an enum plus an explicit `None` for bad
credentials).

### 2. Priority and matching (`AuthService.authenticate`)

1. `ldap_validate` binds `f"{LDAP_DOMAIN}\\{username}"` with the given
   password (`app.infrastructure.ad.client`). `invalidCredentials` returns
   `False`; any other `LDAPException` raises `ServiceUnavailableError` (503)
   — reusing the existing `ErrorCode.SERVICE_UNAVAILABLE`, not a new code.
2. `username in admin_users` (a plain lower-cased set, no AD API call) or
   membership in any `admin_groups` group (checked one group at a time,
   stopping at the first hit) → `Role.ADMIN`.
3. Same shape for `viewer_users`/`view_groups` → `Role.VIEWER`.
4. Otherwise `LoginResult.NO_PERMISSION`.

A user in both an admin and a viewer group gets admin, because admin is
checked first and returns immediately — the spec's "admin wins" rule falls
out of the ordering rather than needing an explicit tie-break. Every
comparison lower-cases both sides.

`AdApiClient` (`app.infrastructure.ad.client`) takes a pre-built
`httpx.AsyncClient` rather than settings directly — the same constructor-
injection shape as `app.infrastructure.openshift.client.InClusterClient`
— specifically so it's testable with `httpx.MockTransport` with no real
socket. `AuthService` itself depends on a `GroupMembershipLookup` Protocol,
not the concrete class, for the same reason one layer up.

**2026-09-23 — the four group/user lists need no API pod restart to
change.** Reported live by the operator: `Settings` is process-lifetime
(`@lru_cache` on `get_settings()`), and an env var change never reaches a
running container anyway (Kubernetes only live-updates a *volume-mounted*
ConfigMap file, never `envFrom`/`env`, without a restart). `AuthService.
_list` now prefers `Settings.<field>_file` (a mounted path) over the
static `Settings.<field>` when set, reading it fresh on every login —
cheap, since `authenticate` runs once per login attempt, not per request.
The chart mounts the whole `api-config` ConfigMap as a volume on the
backend Deployment (`backend-deployment.yaml`, not the CronJobs — they
never call `AuthService`) and points the four `INVENTORY_*_FILE` env vars
at it; `deploy/README.md`'s "Configuration notes" has the full mechanism.
Scoped to just these four values on purpose — `ldap.*`/`adApi.*` and the
session/API-token secrets stay restart-required, since those are rarer,
more consequential changes worth a controlled rollout rather than an
in-place swap under live sessions.

### 3. Session: a stateless, HMAC-signed cookie, not a Redis session

Decided with the operator directly. Redis here is explicitly cache-aside
and non-persistent (CLAUDE.md); a session store there would mean every
Redis restart force-logs-out the whole fleet of operators, which is a
worse failure mode than "an admin demoted mid-shift keeps read+write until
the cookie expires" — accepted below as a known trade-off. The cookie
reuses `app.domain.services.cursor`'s own scheme (HMAC-SHA256 over
base64url JSON, `hmac.compare_digest`) with its own secret
(`Settings.session_secret`, its own dev-insecure default and the same
production fail-fast `cursor_secret` already has) — `app.domain.services.
session`. No JWT library: the payload is three closed fields (`sub`,
`role`, `exp`), so a generic multi-algorithm token format buys nothing.

`POST /api/v1/auth/login` sets it `HttpOnly`, `SameSite=Strict`, `Secure`
only when `environment == "production"` (this repo's own dev config, and
the SPA served over plain HTTP through the Vite proxy, isn't HTTPS).
`GET /api/v1/auth/me` is the SPA's one source of truth on boot:
`{login_required, authenticated, username, role}` — `login_required=false`
(the dev bypass) always reports `authenticated=true, role=ADMIN`, so the
frontend has exactly one code path rather than a separate "auth is off"
branch.

### 4. The dev bypass: `auth.enabled=false` is auto-admin, not a login screen

Decided with the operator directly, against a second option (a dev-only
role picker). `app.dependencies.get_current_actor` returns a fixed
`Actor(id="dev", role=Role.ADMIN)` with no AD/LDAP calls at all when
`Settings.auth_enabled` is false — this repo's own default, since there is
no AD reachable here. Every existing test and the existing Playwright
suite therefore needed zero changes for the auth-disabled path; `tests/
api/test_auth.py`'s `TestDevBypass` class is what pins that.

### 5. Where the gate actually applies

`app.main`'s `include_router(..., dependencies=[Depends(get_current_actor)])`
gates every router except `health`, `auth` and `/metrics` — so a reachable-
but-unauthenticated caller gets 401 on every read too, not just writes.
`require_admin` (`app.dependencies`) layers `role is Role.ADMIN` on top,
applied to exactly the four existing mutation endpoints in `servers.py`:
`PUT`/`DELETE .../maintenance`, `POST .../reclassify`, `POST
.../health/recalculate`. Every read, including `GET /servers/available`,
only needs *a* resolved caller (session or token), not admin.

A bearer token (`Authorization: Bearer <token>`, checked against
`Settings.api_token_admin`/`api_token_viewer` with `hmac.compare_digest`)
is resolved by the same `get_current_actor`, before the cookie check —
this is what lets the BMH generator keep calling `/servers/available` (and
now `PUT .../maintenance`, if it's ever given the admin token) with no AD
login of its own.

### 6. Frontend: shadow, not hide

The operator was explicit mid-build: a viewer must still **see** the
maintenance button and still be able to **filter** by maintenance state —
only the click is a no-op. `MaintenanceToggle` uses `aria-disabled`, not
the native `disabled` attribute, specifically because a disabled button
suppresses the hover event a tooltip needs — `title="Only admins can
change maintenance"` only works with `aria-disabled`. The inventory
filters and the "Maint"/`Stale` chips are untouched; only this one
button's interactivity changes per role.

`AppLayout` gates the whole app on `GET /auth/me`: `login_required &&
!authenticated` renders only `LoginPage`, nothing else. The login page's
visual design (a wordmark, ambient floating server-rack icons, a red/black
accent replacing the app's usual blue "info" hue) was iterated live with
the operator against the running dev server — a deliberate exception to
the app's system-font-only rule (`.claude/rules/frontend.md`), scoped to
this one pre-app screen, not the data-dense interior.

## Deferred

- **Session revocation.** There is no server-side session store, so a
  removed admin/viewer keeps working until the cookie's TTL
  (`session_ttl_seconds`, default 8h) expires. Accepted trade-off of
  choosing a stateless cookie over Redis (Decision 3).
- **Rate limiting / lockout on repeated bad logins** is left entirely to
  AD's own account-lockout policy — `POST /auth/login` has no throttling
  of its own.
- **A real local AD for testing was attempted and abandoned.** Two runs of
  `docker.io/nowsci/samba-domain` (needed because plain OpenLDAP does not
  accept a `DOMAIN\username` simple bind the way AD does) crashed during
  provisioning with `Security context active token stack underflow!` — a
  known Samba ACL/xattr fault under rootless podman, not fixed by
  `--privileged`. `tests/unit/infrastructure/ad/test_client.py` and `tests/
  unit/application/services/test_auth_service.py` mock `ldap3`/`httpx`
  directly instead — real code paths, realistic responses, no real wire
  protocol. A future session with a working rootless-podman-compatible AD
  image (or access to a real one) should replace this with an actual bind.

## Consequences

- Five new settings: `auth_enabled`, the LDAP/AD API block, the four group/
  user lists, `session_secret`/`session_ttl_seconds`, and the two API
  tokens — all documented in `.env.example`, all off/blank by default.
- `Actor` gained an optional `role` field — a backward-compatible addition
  to a stored embedded document (`stored-shape-reviewer` confirms an old
  `audit_events` document still decodes with `role: None`).
- `deploy/helm/server-scan` gained an `auth:` block, a new
  `backend-auth-secret.yaml` (mirroring `collector-credentials-secret.yaml`'s
  `existingSecret` escape hatch), and one more unconditional `envFrom`
  entry on the API deployment.
- CLAUDE.md convention 6 ("no real authentication yet") is now historical —
  the "What's explicitly NOT done yet" list drops real auth entirely.

## Verification

`uv run pytest -q` (39 new tests: LDAP bind + AD API mocked at the
`ldap3`/`httpx` boundary, role-priority and case-insensitivity, session
sign/verify/tamper/expiry, the two new settings validators, and API-level
login/logout/me plus the maintenance role gate and both API tokens), `uv
run ruff check . && uv run ruff format --check . && uv run ty check
backend/app tools tests`, `uv run python scripts/check_comment_density.py`,
`uv run lint-imports`, `helm lint`/`helm template` (default and with
`auth.enabled=true`), and `cd frontend && npm run lint && npm run
typecheck && npm run test -- --run && npm run build` (142 tests, including
a viewer-role render of `MaintenanceToggle` and the `LoginPage` error-
mapping). Manually verified against the seeded dev stack: the dev bypass
(no login page, maintenance toggles), and — via a hand-signed session
cookie for the same reason real AD isn't reachable here — both the login
page and the viewer's shadowed maintenance button with its tooltip.
