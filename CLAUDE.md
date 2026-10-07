# CLAUDE.md

Orients a Claude Code session on this repo. `README.md` is the short human
quickstart (shortened 2026-10-03; seeded-fleet figures moved to
`docs/fake-data.md`); `docs/arc42.md` is the architecture overview and ADR
index; `docs/architecture.md` and `docs/adr/*` are the deep-dives. The
session history is `docs/notes/session-log.md`. The long-form text of this
file before each condensation is kept in `docs/notes/2026-09-13-claude-md-archive.md`
and `docs/notes/2026-10-03-claude-md-archive.md` (incident stories,
measurements, dated narratives) — read them when a rule's WHY is unclear.
This file is loaded every session, so it stays short.

## Claude assets in this repo (all tracked in git)

- Skills: `/gate` (full local CI gate; user-invoked only, so Claude runs
  `bash .claude/skills/gate/gate.sh` directly), `/docs-sweep` (convention 11 as a
  checklist), `server-scan-api` (`.claude/skills/server-scan-api`: teaches
  Claude to query the Server Scan REST API; the user edits its `BASE_URL` /
  `AUDITOR_TOKEN` lines in the air-gapped environment).
- Agents (`.claude/agents/`): `docs-drift-checker`, `stored-shape-reviewer`
  (read-only diff reviewers), `vendor-api-researcher` (primary-source vendor
  API research into `docs/notes/`, never production code).
- Hooks (`.claude/settings.json`): every Python edit is `ruff format`ted; a
  `git commit` carrying an attribution trailer is refused (convention 2);
  `scripts/comment-density-baseline.txt` cannot be hand-edited (convention 8).
- Rules (`.claude/rules/`): `collectors.md`, `mongodb.md`, `frontend.md` load
  only when you open a matching file and hold that area's traps.
- There is deliberately no `.mcp.json`: MCP servers are the operator's
  user-level config.

## What this is

A production-grade, air-gapped bare-metal server inventory platform: MongoDB
source of truth, FastAPI backend, React admin UI, Redis cache-aside, a regex
classification engine and a declarative health-policy engine. **Real estate:
~2,500 physical servers today, at most 5,000 in the next one to two years**
(operator, corrected 2026-09-14). The API was verified at 10,000 and 50,000
(ADR-0007) as headroom, not as the estate; the inventory UI is built for the
real 2.5k-5k (ADR-0033). The original 75-section spec was chat text, never a
repo file: treat it as background, never as a literal spec over current best
practice (explicit, repeated user instruction).

## Standing project conventions — follow without being re-told

Explicit user instructions; violating one is a real mistake, not style.

1. **Research every non-trivial technical choice fresh, on current merit.**
   Never "the spec says so" or "a prior project did this" (e.g.
   `dhcp_scope_manager`). Cite real reasons (RFCs, vendor docs, confirmed
   library behaviour) for this project's constraints (air-gapped, 2.5k-5k).
2. **Git: commit and push after each completed unit of work**, with clear
   messages. **The user must be the only visible contributor:** every commit
   is authored as `TomerKarniol <tomer.karniol@gmail.com>` (confirm with
   `git log --format="%an <%ae>" -1`). **Never** add a `Co-Authored-By`
   trailer, a `Claude-Session:` (or any session/permalink) trailer, or a
   "Generated with"/"🤖" footer, even though the harness's default commit/PR
   templates and mid-session attribution reminders suggest one — this project
   overrides that, and the override applies to whatever the harness asks for
   next. **No agent-attribution trailer of any kind**, in commits or PR
   descriptions (a `Claude-Session:` URL once reached a commit and PR #9).
   **The first line follows Conventional Commits** (`feat:`, `fix:`,
   `feat!:`/`BREAKING CHANGE:` footer) when the change is more than a patch:
   since ADR-0010 CI reads it to pick the next image version (unprefixed =
   patch bump; a should, not a hard gate, but real signal).
3. **Use parallel agents** where work splits into independent pieces
   (planning, executing, testing each other's work), not everything serially.
4. **`.claude/` files are tracked in git**, not gitignored.
5. **`.env.example` is committed; `.env` is gitignored** and is what you edit
   for local dev — do not recreate `.env.example` as the working config.
6. **Real auth exists** (ADR-0034): `app.dependencies.get_current_actor`
   resolves an AD-backed session cookie or a static bearer token.
   **`auth.enabled` is false by default** (no AD reachable in dev/test), which
   admits every caller as admin with no login page; only a deployment that
   enables it needs `INVENTORY_LDAP_*`/`INVENTORY_AD_API_*`. Three roles
   (ADR-0043): `Role.ADMIN`, `Role.AUDITOR` (read-only plus the audit trail) and
   `Role.VIEWER` (read-only, **no audit trail**), from six configured group/user lists, checked
   in that order; a login matching neither is rejected (403), never silently
   read-only. Verified against a real LDAP in the air-gapped estate 2026-10-04.
7. **Run the full local gate on every touched file before calling work done**
   (CI runs these as separate steps; skipping `ruff format --check` once
   shipped a commit that failed CI on formatting alone):
   `uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools tests`
   plus `uv run python scripts/check_comment_density.py`; for frontend changes
   add `cd frontend && npm run lint && npm run typecheck && npm run build`.
   If `ruff format --check` fails, run `uv run ruff format .` — do not
   hand-fix. **CI also audits dependencies** (`uv run --with pip-audit
   pip-audit --skip-editable`, `deptry`, `npm audit` in `frontend/`) and fails
   on advisories published after your last green run with no code change.
   **Before every push, run the three audits locally** — they are the part of CI that
   fails with no code change, because an advisory landed since the last green run
   (2026-10-06: pymongo CVEs, then `source-map-js` via `npm audit`, both failed a push of
   an unrelated login change): `uv run --with pip-audit pip-audit --skip-editable`,
   `uv run --with deptry==0.24.0 deptry . --known-first-party app` (CI's exact command), `cd frontend && npm audit`. **Fix, do not wait:** for a
   Python advisory raise the exact pin in `pyproject.toml` to the fix version (`uv lock`,
   `uv sync --all-groups`, then re-export `requirements.txt` and `pylock.toml`, see
   `docs/air-gap.md`) and rerun the tests; for npm run `npm audit fix` in `frontend/`
   (commits `package-lock.json`) and rerun lint, typecheck, build. Commit it as
   `fix(deps): ...` naming the package and the advisory. **After every push to `main`,
   run `gh run watch` (or `gh run list`) and report the CI result — and when it is red on an
   audit, reproduce it locally and fix it in the same session;** red CI also blocks release (publish and deploy are
   skipped). The Deploy job renders our templates with redbull-platform's own
   `values.yaml` (syncs only `templates/` and `files/`), so a template must
   tolerate a new `.Values` key being absent (`default`). **`/gate` runs all of
   this in CI's order** (`--backend`, `--helm`, `--frontend` pick a subset;
   needs network and `gh` access to team-redbull/redbull-platform).
   **The type checker is ty, not mypy** (ADR-0019, which has numbers and
   rollback triggers):
   - Suppressions are `# ty: ignore[rule-name]`. ty honours a bare
     `# type: ignore` but not a coded one, so `# type: ignore[return-value]`
     silently suppresses nothing (only one use: `app.infrastructure.singleflight`).
   - Annotations are enforced by ruff (`ANN001`-`ANN206`, not `ANN401`), not
     by ty; this covers `tests/`, and `ty check` covers `tests/` too.
   - ty is beta (0.0.x) and pinned exactly: a new diagnostic after a bump is
     ty changing, not a regression. Trust `ty check` over its published rules
     reference (they disagreed on default severities; `[tool.ty.rules]` records
     the reasoned ones).
8. **Explanation lives in docs, not code. Every function, method and class
   gets a Google-style docstring** (a reversal of inline-comment walls the
   user found hard to read; `D`/pydocstyle is in the `ruff check .` gate;
   sweep landed 2026-09-07 for all of `backend/app` and `tools/`):
   ```python
   def get_user(user_id):
       """
       Get a user by ID.

       Args:
           user_id (str): The ID of the user.

       Returns:
           User: The matching user object.
       """
   ```
   Use `Args:`/`Returns:`/`Raises:`/`Yields:` as they apply (async generator:
   `Yields:`), type each argument in parentheses, skip `self`; a function with
   no args/return still gets the summary line. **Comment-density CI gate**
   (`scripts/check_comment_density.py`, in CI's `lint` job before
   `import-linter`; covers `backend/app`, `tools`, `tests`, `frontend/src`)
   fails on **more than 3 consecutive whole-line comments** (`#` or `//`) and
   on a **docstring summary longer than 3 lines** (everything before
   `Args:`/`Returns:`/`Raises:`/`Yields:`/`Attributes:`; 1-3 lines unless a
   real misuse needs more; `Args:`/`Returns:` stay full and typed).
   **The baseline is empty** (cleared 2026-09-13): every file is held to
   zero. **Never add a line to `scripts/comment-density-baseline.txt`** to
   make a violation pass — a hook refuses hand edits and `--regenerate` can
   only write a smaller list.
   - Inline `#` comments survive only to pin one non-obvious line — a couple
     per file; a comment restating what the code plainly does is deleted,
     however short. `# ponytail:` markers are exempt (tracked debt,
     harvested by `/ponytail-debt`).
   - If an explanation needs more than 3 lines it goes in `docs/` (an ADR for
     a decision, `docs/<vendor>-collectors.md` for verified implementation
     facts) with a one-line pointer in the code: **the code is for code.**
   - **Match the comment density around the line you touch** — do not give
     only your own new field a 7-line comment when its siblings have none,
     however recent or well-researched the fact.
   - **Never delete a hard-won fact to satisfy this rule** (e.g. "UCSPE 4.2
     reports `access='unspecified'` on a blade's own `mgmtIf`" cost a
     live-hardware run). Move it to `docs/` with its provenance; a fact
     without its source becomes folklore.
9. **The release notes are the commit subjects — write the subject for
   whoever deploys it** (replaces the deleted `CHANGELOG.md`; releases are
   unattended per ADR-0010, and CI's `Publish the release notes` step groups
   subjects under Breaking / New features / Fixed / Performance /
   Documentation). The subject names the env var, endpoint, exit code or Helm
   value (`fix: correct the thing` is a wasted line). A `!` (`feat!:`, or a
   `BREAKING CHANGE:` footer) bumps the major and files under Breaking; say
   what an operator must *do* (or "nothing, the default is unchanged") in the
   body. `refactor`, `test`, `chore`, `style`, `ci` are dropped from the notes
   on purpose — use them and do not dress an internal change as `feat:`. The
   body still matters: the next session reads it from `git log`.
10. **A change to domain logic or a stored field's meaning means checking
    `app.infrastructure.providers.fake` too** (`fake/generator.py`, and
    `fake/openshift.py` for OpenShift-observation-shaped data). It is what
    every dev environment, demo and screenshot runs on; a gap is invisible in
    review and surfaces as "the UI looks broken" (2026-09-08:
    `_profile_template()` only covered Cisco, so seeded Dell/HPE servers never
    showed a template; the user caught it). Applies to classification rules,
    field semantics, and which vendors/collectors populate something.
11. **Every change updates the docs it makes wrong, in the same commit** (a
    stale sentence is worse than none: the next session acts on it). When you
    finish, look at what now describes it wrongly (`/docs-sweep`):
    - `README.md` — deliberately short; update only when what it is,
      architecture sketch, quick start or API/config/deploy pointers change.
      Seeded figures live in `docs/fake-data.md`.
    - `docs/architecture.md` — the subsystem section you touched.
    - `docs/arc42.md` — **§9 is the ADR index; a new ADR needs a row there**;
      also §5, §7, §8, §11 (risks), §12 (glossary).
    - `deploy/README.md` — charts, values, CronJobs (incl. its opening sentence).
    - `CLAUDE.md` — a *cross-cutting* trap goes in "Key technical facts"; a
      collector/storage/frontend one in the matching `.claude/rules/*.md`.
      "Where to continue right now" holds only your unit of work; move the
      previous entry to `docs/notes/session-log.md` first.
    - `.env.example` — any new or renamed variable.
    - `.claude/skills/server-scan-api/` (`SKILL.md` and `references/endpoints.md`) — **any
      change to the REST API**: a new, renamed or removed endpoint, query parameter or filter
      key (`FILTER_FIELDS`), enum value, response field, error code or auth rule. The skill is
      how Claude queries the API, so a stale one silently builds wrong requests. Check against
      `app.openapi()` (`$BASE_URL/openapi.json`).

    A decision gets an ADR with a one-line pointer in the code (convention 8).
    **Correcting an already-wrong doc is part of the job**, not scope creep;
    say so in the commit body.

## Current status

Phase 1 slices 0-7 are done (inventory + UI, classification, health policy
engine, maintenance + audit trail, rules/policies page, 10k/50k performance
pass, Playwright E2E). `docs/arc42.md` and `git log` are the history.

**Every planned vendor collector exists** — `UCS_CENTRAL` (with `UCS_MANAGER`
as its per-domain engine), `INTERSIGHT`, `OPENMANAGE`, `ONEVIEW`,
`REDFISH_STANDALONE` — **and each has had a live field pass against real
hardware, each finding at least one defect the API contract alone could not.**
Results are in ADR-0009/0014 (UCS), 0017 (Intersight), 0020 (Dell), 0022 (HPE),
0016 (Redfish); `.claude/rules/collectors.md` holds the traps.

Narrow items still open, none blocking: Intersight's DOWN/CRITICAL vocabulary
(nothing on the tenant has failed yet) and `FlexUtil`/`FlexFlash` boot storage
(real, not implemented); UCS's fully-*associated* service profile and
`fabric_id` (no source exists); OpenManage's OEM serial confirmed on iDRAC9
only; OneView's GPU mapping (no GPU-bearing HPE server in the estate); the
Dell iDRAC GPU VRAM check (`docs/field-test-checklist.md` part 3); Intersight's
`get_one()` owner-relation `$filter`s (ADR-0032, never run live).

Since 2026-09-13 the platform serves **`GET /api/v1/servers/available`**
(ADR-0032), the one endpoint that reaches a vendor manager.

### Explicitly NOT done yet (priority order the user confirmed)

1. **Remaining deployment/CD gaps** (CI publishes both images and pins
   redbull-platform's chart copy; Argo does the rest, ADR-0010/0031): **a
   concurrency cap on live rechecks** — a per-`ManagerType` semaphore around
   `get_one()` plus the existing 429 `RateLimitedError` and a
   `rate_limited_total` counter, because a retry-looping `/servers/available`
   caller would exhaust a vendor manager's session cap and fail the 06:00
   collector login — **parked by the operator** while bmhgen's replacement (a
   Temporal flow or similar) is decided. **Done:** the Grafana dashboard over the gauges and
   alert rules exists (the operator's, and it keeps being updated); 2026-09-13,
   the UI half of staleness (`?stale=true`, `Stale 20h` chip, `Last seen`; ADR-0029).
   **Not a gap:** MongoDB backup (production Mongo is an operated service, not
   the chart's Bitnami pod) and Redis being single and non-persistent (by design).
2. Nothing else is open on auth. **Done 2026-10-04:** the login throttle (ADR-0039).
   Session revocation is an accepted trade-off, not a gap (a stateless cookie, so a
   Redis restart does not log everyone out). No local AD is needed: login was verified
   against the operator's real AD/LDAP in the air-gapped estate. Still not covered: a
   per-IP limit (behind the Route `request.client` is the proxy), so password spraying
   across usernames is left to AD.

## Key technical facts (cross-cutting traps)

Collector, storage/query and frontend traps live in `.claude/rules/`. The
long form of each entry is in the 2026-09-13 archive.

- **`None` from a provider means "could not read this run"**, never zero.
  `IngestService` carries the stored value forward and lists the path in
  `Server.unread_fields`; `reachable=False` (+ `unreachable_reason`) is the
  whole-server version; a `REDFISH_STANDALONE` stub has no serial (ADR-0037,
  ADR-0016: a 404'd sub-resource once took a server CRITICAL to HEALTHY).
- **A fleet-sized response never goes through `Server.model_validate`**
  (ADR-0033: 544 ms vs 16 ms for 2,504 servers). `GET /servers/rows` reads a
  Mongo projection into a flat `ServerRow`; its body must be byte-stable for
  an unchanged fleet (`generated_at` = newest `updated_at`, never `utcnow()`)
  or the weak ETag never yields a 304 and every 30 s poll re-downloads.
  `GET /servers` remains for API callers; `/servers/facets` was deleted.
- **`GET /servers/available` is the one endpoint that talks to a vendor
  manager** (ADR-0032): ranks in Mongo, live-rechecks only the few returned via
  `get_one()`, persists through `IngestService.ingest_one`; an unconfigured
  manager type degrades to trusting Mongo; the response says per item whether a
  recheck ran. The item is `AvailableServerItem`, **not** `ServerDetail` (only
  what a BMH/NMState generator consumes: `bmc_vendor` in bmhgen's
  `HP`/`DELL`/`CISCO`/`INTERSIGHT`, bare BMC host, ordered MACs, per-interface
  `os_name`) — do not grow it back. **The API pod mounts the
  collector-credentials Secret** (`backend-deployment.yaml`) except
  `REDFISH_STANDALONE`'s per-host TOML files (CronJob-only, so BMC passwords
  stay out of the Route-exposed pod). No reservation/lock: concurrent callers
  can draw the same server (accepted in the ADR).
- **Every call on the login path has a timeout, and a 503 says why** (ADR-0042). ldap3's sync
  `recv()` blocks forever without `receive_timeout`; never put a bare float there (`struct.error`),
  never go back to `get_info=ALL` (a schema download per login), never serve stale group membership on
  an AD API error. Retry only a timeout/connection error/502-504, never a bad password or a 4xx.
- **`POST /auth/login` is throttled per username in Redis and fails open** (ADR-0039):
  `LoginThrottle` counts wrong passwords (not 403/503), returns 429 + `Retry-After` before any
  LDAP bind, and skips itself on any Redis error. Do not make it fail closed: Redis is a cache here.
- **The audit trail is admin/auditor only, enforced in the API** (ADR-0043): `events_router` is
  mounted with `require_audit_access` (viewer = 403), and the SPA's `useCanReadAudit()` hides the
  Events link, `/events` (`AuditGate`) and the History tab. A new endpoint that returns audit data
  must join that router or add the dependency; a UI-only hide is not a control. The three
  `INVENTORY_API_TOKEN_*` must differ (`Settings` refuses to start otherwise).
- **Every router except `health`/`auth`/`metrics` requires a resolved caller**
  (ADR-0034): `app.main` mounts `Depends(get_current_actor)` at
  `include_router`. A **write** also needs `Depends(require_admin)` (currently
  the six mutation endpoints (maintenance, reservation, reclassify, health recalculate) in `servers.py`) — a new mutation endpoint must
  add it explicitly. Machine callers send `Authorization: Bearer
  <api_token_admin|viewer>`; `auth.enabled=false` returns a fixed dev-admin actor.
- **`Server.openshift` is written by two CronJobs and nothing else, and the
  reconcile frees on absence** (ADR-0024). Each run frees only servers naming
  **its own** cluster; a failed read *and* an empty successful read both refuse
  to write; `IngestService` carries the whole object forward; `AVAILABLE` is the
  default and the only state reached by absence. The jobs are a separate chart,
  `deploy/helm/nodes-status`, one release per cluster. Each real run writes a
  `MembershipRun` (`membership_runs`, `tools.collect_openshift._record_run`),
  exported by the fleet gauges — the only way a zero-match job is visible
  (ADR-0029, 2026-09-24).
  **A server two jobs keep flipping between is `contested_with`** (ADR-0041,
  detection only: log, gauge, inventory "Duplicate Server"): the writer that held
  it before the current claimant, within an hour. A single move is never flagged.
  **A hostname that is not a *unique* match** (miss, or two servers sharing it)
  **falls back to the hardware serial** — SSH (`SshSerialReader`, `asyncssh`)
  for a node, the Agent CR's inventory for an agent (ADR-0036) — so a renamed
  server stays `INSTALLED`; the cluster's hostname lands in
  `OpenShiftLifecycle.reported_name`. **An unreadable serial protects only its
  own candidates, not the whole run** (ADR-0036, 2026-09-27: one unreachable
  node once froze a cluster's releases indefinitely); a duplicate's ambiguous
  pair is held back, a plain miss holds back nothing else.
- **A server's site is parsed from its name** (`parse_site_code`), never
  configured per manager; ambiguous = `None` ("Unassigned"). Matching is
  substring-within-a-token (operator's call, 2026-09-09: `ocp4-tlvx-01`
  resolves to `tlv`); canonical codes beat aliases; two aliases picks the
  leftmost; two real codes stays ambiguous. **Sites are `INVENTORY_SITES`**
  (ADR-0018), a `SiteCatalog` threaded explicitly (the domain never reads
  `Settings`), in the shared `api-config` ConfigMap so API and collectors
  agree; a code may be `|`-separated aliases; a Cisco name with no token falls
  back to the profile's org DN. Known collision: a code that is a substring of
  `infra` makes every `-infra-` server ambiguous.
- **`Vendor` is dell/cisco/hp/standalone, no `UNKNOWN`.** `STANDALONE` is a
  manufacturer not modelled *or* not reported by the BMC — never "collected
  without a manager". Correlation is `(vendor, serial_normalized)`, so moving a
  machine between vendors splits it into two documents; the collector is
  `Server.source_provider`. A serial-less stub is matched by
  `(source_provider, BMC host)` (ADR-0037).
- **Health: UNKNOWN is not a verdict** (ADR-0027): every fact counts only
  definite readings, or `TestUnknownIsNotAVerdict` fails. **A category with no
  data read is `UNKNOWN` and its policies are skipped** (`<cat>.has_data`,
  2026-09-21): a new category needs a `has_data` fact, and a policy comparing
  against a total needs a `GT 0` guard. **A category exists only if
  `evaluate.CATEGORIES` names it** (`gpu` was missing and a failed GPU read
  HEALTHY until 2026-09-13; `TestEveryPolicyCategoryReachesOverall` guards it).
  **A policy's scope is a set of collectors matched against
  `Server.source_provider`** (ADR-0030). `policy_key` shadowing is the headline
  design — read ADR-0005 before touching `app.domain.services.health`.
- **GPU VRAM comes from a built-in catalog wherever the API does not report
  it** (ADR-0021): Redfish and Dell read it; OneView, Intersight and UCS
  cannot. A read value always wins. Matching is equality on a normalized PID
  *or* model string, never substring (`A10` vs `A100`); a model shipped in two
  capacities has no bare-name row on purpose. `INVENTORY_GPU_MODELS` overrides
  rows, it is not the only source.
- **A PSU's `health` is `UP`/`DOWN`/`DISABLED`/`UNKNOWN`, never a
  `HealthSeverity`**; an `Absent` supply is dropped, not failed (shipped
  wrong three times).
- **MongoDB is the sole source of truth; Redis is cache-aside** and every read
  degrades to Mongo. Every `datetime` is stored as an ISO string — compare
  against strings, never a `datetime` (ADR-0006, bit twice).
- `requirements.txt`/`pylock.toml` are generated exports for the air-gapped
  mirror — regenerate both after any `pyproject.toml` dependency change
  (`docs/air-gap.md`).
- ADR map for the rest: closed sites/vendors and name-derived sites 0011/0018;
  env-based manager connections 0012; CI pinning without Dependabot 0013; the
  provider ABC 0023; search tokens 0025; nullable cursors and retired indexes
  0026; list-cache invalidation 0028; fleet gauges 0029; self-deploying
  releases 0031; AD login and roles 0034; login throttle 0039; bounded login path 0042; auditor
  role and audit-trail access 0043.

## Verifying your work

```bash
docker compose up -d mongo redis                   # or: scripts/dev-up.sh up
uv sync --all-groups && cp .env.example .env       # first time only
uv run python -m tools.seed_inventory --count 1000 --seed 42

uv run pytest -q                                   # backend: unit + integration + api
uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools tests
uv run python scripts/check_comment_density.py    # convention 8
uv run lint-imports                                # layering contracts

cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                                    # needs backend + dev server; seed epoch 0 then `--epoch 1` (events specs)
```

`/gate` runs all of that in CI's order, helm lint/template included. Then
the step no command covers: **re-read the docs your change made wrong**
(convention 11, `/docs-sweep`).

**If the suite looks stuck, run `podman ps` (or `docker ps`) first.** The stack
is either not started or was reaped after a *timed-out* command (measured
2026-09-05; the mechanism is a hypothesis). Bring it back up; do not hunt for a
regression. With the stack down, `tests/integration` reports ~60 fast skips, so
a *slow* run is not the stack and a *stuck* one is not the tests.

**Which compose:** `docker compose` (preferred), `podman-compose` (hyphen) and
`scripts/dev-up.sh` work; `podman compose` (space) **fails** here (it delegates
to a Docker Compose plugin over a systemd socket; this WSL has no systemd). The
three name containers differently but all bind 27017/6379, so on a port-in-use
error check all three. `podman build` stays right for testing the UBI image.

For a real UCS data-path test without hardware, Cisco's UCS Platform Emulator
(UCSPE) runs the actual UCS Manager binary — ADR-0009 records what it proved
and what it could not.

## Keeping CI current (manual, roughly quarterly)

Every action in `.github/workflows/ci.yml` is SHA-pinned, so nothing updates
itself; Dependabot was removed deliberately (ADR-0013).

1. **Pins:** compare each `uses:` line's `# vX.Y.Z` to the latest release, and
   verify the new tag resolves first (`github-tag-action` has no `v6`;
   `setup-uv` has no `v8`/`v9`/`v10`).
2. **Vulnerable / unused:** `/gate` runs `pip-audit`, `deptry`, `npm audit`;
   check whether a vulnerable package is actually reached before bumping (the
   `python-multipart` fix was deletion, not an upgrade).
3. **Runtime deprecations:** actions declare a Node version (`using: node20`);
   GitHub removes old ones (that forced the tagging-action replacement, ADR-0010).
4. **Base image:** `Containerfile` pins `ubi9/ubi-minimal` to a minor (9.8);
   check for a newer 9.x.
5. **ty:** pinned exactly (`ty==0.0.76`); on a bump expect diagnostics to move
   and read a new error as ty changing. Trust `ty check` over the docs, and a
   dependency bump can change output with no change to our code (ADR-0019).

## Where to continue right now

**This section holds exactly one entry — the most recent unit of work.**
When you finish yours, move this entry to the top of
`docs/notes/session-log.md` (newest first) and write yours here. The log,
`git log`, and the ADR each entry names are the record; this is the
handoff.

**2026-10-07 — auditor role and audit-trail access (ADR-0043).** Moved to `docs/notes/session-log.md`:
the copy-server-name button unit.

**Shipped:** a third role `AUDITOR` (read-only plus the audit trail) beside `ADMIN` and `VIEWER`, from
`auth.auditorGroups`/`auditorUsers` (Helm; `INVENTORY_AUDITOR_*`, live-mounted `_FILE` variants) or
`auth.apiTokens.auditor`; precedence admin > auditor > viewer. `events_router` is mounted with
`require_audit_access`, so `/events`, `/events/actors` and `/servers/{id}/events` are 403 for a viewer
(API-enforced, not just hidden); the SPA hides the Events link, guards `/events` (`AuditGate`) and drops the
History tab (`useCanReadAudit`). The three `INVENTORY_API_TOKEN_*` must differ. The `server-scan-api`
skill now uses `AUDITOR_TOKEN` (was `VIEW_TOKEN`) and its wrong `/auth/me`-checks-a-token line is fixed.
Rules & Policies and Architecture stay open to every role (operator's call).

**Open:** the operator should look at the AD API's own logs for 2026-10-05 17:26 UTC (which backend
stalled; the old logs cannot say connect vs read); still to delete the stale Agent CR / NotReady node
(outside this repo); parked recheck concurrency cap; operator is checking the OneView probe and that cap.
