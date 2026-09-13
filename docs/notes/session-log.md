# Session log — what each unit of work shipped and surfaced

CLAUDE.md's "Where to continue right now" keeps only the single most
recent unit of work. When you finish one, move the entry that is there
down to the top of this file and write yours in its place — newest first.
`git log` and the ADR each entry names are the authoritative record; this
is the narrative a session reads to pick up where the last one stopped.

---

**Before that, 2026-09-13, evening — the stale filter, `INFO` retired, and a layering fix.**
The inventory gained `?stale=true` (a `$$NOW`-based `$expr` so the cursor
binding stays constant — `.claude/rules/mongodb.md`), a `stale` flag on
every server response, a `stale` facet, a `Stale 20h` chip in the State
column and a relative `Last seen` on the detail page (ADR-0029 update).
`HealthSeverity.INFO` is gone: no shipped policy ever produced it; a
stored `INFO` decodes as `HEALTHY`. And the morning's commit had made
`app.api` import `tools.run_collector` — the CLI layer above it — which
worked in the container and CI only because uvicorn's default `--app-dir .`
puts the repo root on `sys.path`, and broke the README's documented
`--app-dir backend` command; provider construction now lives in
`app.infrastructure.providers.factory` and both callers import it from
there. Verified in a real browser (Playwright against the seeded dev
stack): a `Seen` column was built, measured to push Maintenance off a
1440px viewport, and removed. Earlier the same day:

**2026-09-13 — `GET /api/v1/servers/available`** (ADR-0032). A read API for
`BareMetalHostUCS`'s BMH-creation flow to call instead of querying HP
OneView / Cisco UCS Central / Dell OME / Cisco Intersight live itself:
`?name=` for one exact server; `?pattern=` (a real MongoDB regex,
capacity-token-aliased — `5tb` also matches a bare `hypershift` server,
`10tb` a `hypershift-data` one) for a health-tiered, randomly drawn,
`?count=`-bounded set; `?vendor=`/`?source_provider=` to narrow either.
Each item is a purpose-built `AvailableServerItem` carrying only what
`bmh-generator-operator` consumes (second commit, same day, after reading
its generators). Candidates come from Mongo; only the few being returned are live-verified,
via a new sixth abstract method `get_one(ServerIdentity)` on
`ServerInventoryProvider` (implemented in all seven providers) and a new
`IngestService.ingest_one`. The API pod now mounts the
collector-credentials Secret for this; an unconfigured vendor degrades to
trusting Mongo. Shipped with it: `INVENTORY_MAX_AVAILABLE_COUNT` and
`INVENTORY_CAPACITY_ALIASES` (Helm `config.maxAvailableCount`/
`.capacityAliases`), a `flake8-bugbear` allow for FastAPI `Query`/`Depends`
defaults, and this CLAUDE.md restructure — three path-scoped rules under
`.claude/rules/`, the history moved to `docs/notes/`. **Open:** Intersight's
`get_one()` owner-relation `$filter`s have never run against a live tenant
— the next `verify_intersight` pass should exercise one.

**Before that, 2026-09-13, late — the comment sweep.** Eight parallel
agents cleared every one of the 701 comment-density violations and then
deleted every short comment that merely restated the code: 193 files,
−3,600 lines net, the baseline now empty (convention 8). Every displaced
fact went to its topical doc — `docs/cisco-collectors.md`,
`docs/dell-collectors.md` (new "NICs" section), `docs/hpe-collectors.md`,
ADR-0016's dated update (Redfish implementation facts), ADR-0007/0012/0026
updates, `docs/architecture.md` (the fake provider's shape, the provider
contract, the default-policy table, how `run_collector.py` is put
together, the `default_system_rules` ordering history), `deploy/README.md`
and `.env.example`. Six stale statements were corrected on the way. Two
things it surfaced: `Manager` carried five never-written, never-read
fields (`site_id`, `parent_manager_id`, `bmc_credential_ref`, `metadata`,
`ALLOWED_PARENT_TYPES`) plus an index on one of them — removed the same
night, index retired via `RETIRED_INDEXES`, old documents load unchanged
(`tests/integration/test_manager_repository.py`); and keyset paging
on `updated_at`/`last_seen_at` returned an empty second page (a real
`datetime` in `$gt` against ISO strings — ADR-0006's trap), confirmed
live and fixed in the commit after the sweep.

**Before that, 2026-09-13, night — releases deploy themselves** (ADR-0031).
CI gained a `deploy` job after `publish`: it checks out redbull-platform
with `REDBULL_WRITE_TOKEN`, `rsync`s the chart's templates/files into
`gitops/charts/server-scan`, `yq`s the two image tags and `appVersion`,
renders offline, and pushes one `chore(server-scan): pin images to X`
commit with a rebase-retry. The gitops `values.yaml` is never replaced.
Also that evening: the GPU category was never rolled into overall health
(a DOWN GPU read HEALTHY) — fixed in v1.1.3, see "Key technical facts".

**Before that, 2026-09-13, evening — v1.1.0** — health policies are scoped
to a *set* of collectors and the Rules & Policies page groups them by
scope (ADR-0030). `PolicyScope.manager_types` (list, empty = everyone)
replaces `manager_type`; the two UCS fabric-path defaults are scoped to
`vendor=cisco, manager_types=[UCS_CENTRAL, INTERSIGHT]`, everything else
is general; the page shows "General" first, then "Cisco — UCS Central,
Intersight", each sorted CRITICAL → MAJOR → WARNING. The real fix
underneath: **a manager-scoped policy had never matched any server** —
every evaluation call site passed `manager_type=None` — and
`Server.source_provider` is now threaded through as that value. See the
"Key technical facts" entry. Same evening: `/gate` lost its
`disable-model-invocation` flag so a session can run it itself, which
was the point of it.

**Before that, 2026-09-13, later** — **the repository is
`team-redbull/server-scan`**, renamed from `server_scan`. Every `v*` tag
and GitHub Release up to v17.4.3 was deleted at the operator's direction
and versioning restarted: the first release under the new name is
**v1.0.0**, and the images are `ghcr.io/team-redbull/server-scan-api` /
`-frontend` (the old `server_scan-*` packages are deleted from GHCR).
ADR-0010's dated update records it. Two things came out of the same
afternoon: the publish job is now re-runnable after a failed step — a
GitHub API outage left a tag with no release and no images, and the
"version moves forward" guard then refused the re-run until the tag was
deleted by hand — and redbull-platform was pinned to `1.0.0` and
deployed (Synced/Healthy). The operator also stated the real estate:
**~5,000 servers today, up to 10,000** — the scale statements in this
file, `README.md`, `docs/architecture.md` and `docs/arc42.md` now say
that, with the 50k verification kept as the measured headroom.

**Before that, 2026-09-13** — the Claude Code setup itself: `/gate`,
`/docs-sweep`, three enforcing hooks and two review agents, all under
`.claude/` (tracked, per convention 4); MCP servers stay user-level. See convention 7 for what each does. Nothing in the
platform changed.

**Before that, 2026-09-12, later the same day** — staleness
detection (ADR-0029), item 0 of the not-done list: fleet gauges on
`/metrics`, a `ServiceMonitor` + `PrometheusRule` in the chart, and the
frontend's nginx collapsed to three `location` blocks. Also the same day:
maintenance is switched only from the inventory list now (the detail page
is read-only for it), the Name column is left-aligned with everything
else centred, and the Helm chart's fake collector seeds 2,500 servers to
match the operator's real estate.

**Earlier the same day** — eight operator-requested changes, all
shipped. The deployment ones first:

- **The Helm chart is `deploy/helm/server-scan`**, renamed from
  `server-inventory`, along with `app.kubernetes.io/part-of`, the
  `serverScan.*` template helpers, the project name in `pyproject.toml`,
  `INVENTORY_SERVICE_NAME`, `scripts/dev-up.sh`'s pod name, and **the
  Mongo database and user, both now `server-scan`**. The database rename
  was made at the operator's explicit direction after the orphaning risk
  was raised: **an existing deployment's data stays in the old
  `server_inventory` database and must be moved by hand** — `mongodump
  --db server_inventory` then `mongorestore --nsFrom 'server_inventory.*'
  --nsTo 'server-scan.*'`. A hyphen in a Mongo database name is legal and
  was verified against a real server, not assumed; only the `mongosh`
  shell needs `db.getSiblingDB("server-scan")` rather than dotted access,
  since `db.server-scan` parses as subtraction.
- **Exactly one Route, and it does not gain one per endpoint.** The Route
  only gets traffic into the cluster; the frontend's nginx decides which
  paths belong to the API and forwards them to its Service
  (`frontend-api-proxy-configmap.yaml`, mounted at
  `/etc/nginx/api-proxy.d`, which `frontend/nginx.conf` includes). So
  `route.apiPaths` and the five path-scoped API Routes are gone, and
  **exposing another API path is one `location` block there** — no second
  Route, no second hostname. Currently forwarded: `/api/`, `/health/`,
  `/metrics`, `/docs`, `/redoc`, `/openapi.json`. It is a list rather
  than a catch-all because the SPA owns `/` and has its own client-side
  routes (`/servers`, `/rules`, `/health-policies`) that must not be
  proxied.
- **The standalone Redfish TOMLs live in the chart**, at
  `deploy/helm/server-scan/files/redfish/`, read with `.Files.Get`
  (`collectors.redfishStandalone.inventoryFile`/`credentialsFile`). The
  inventory file is the default source; **the credentials file is opt-in
  and empty by default because rendering it puts BMC passwords in git**,
  and `credentialsSecret` still wins.
- **`helm template` passing proves nothing about a missing value.** Helm
  renders an absent `.Values.x` as an empty string with no warning, so a
  mis-nested values file (a block inserted between a map's `enabled` and
  its other keys — done twice on 2026-09-13, in this repo's own
  `values.yaml` and again in redbull-platform's copy) rendered
  `expr: server_scan:collector_silent_seconds >` and sailed through
  `helm lint`, `helm template` and `promtool`. Only OpenShift's
  `prometheusrules.openshift.io` admission webhook rejected it, at Argo
  sync time. Two things now stand in the way: every threshold the
  PrometheusRule reads is wrapped in `required`, so the render fails
  with a message naming the key; and **before pushing a chart change,
  run it against the real cluster** — `helm template ... | oc apply
  --dry-run=server -f -` exercises every admission webhook, which nothing
  offline can.
- **CI has a `helm` job** that lints every chart under `deploy/helm`
  (discovered, not listed) and `helm template`s each one under the value
  combinations the defaults never reach. It uses the runner's
  preinstalled helm rather than `azure/setup-helm`, so ADR-0013's
  SHA-pinning obligation gains nothing new to maintain.

And the four application ones: the Unassigned site card is hidden while empty (a configured
site still shows at zero); the Redfish credential circuit breaker is
deleted so every BMC is attempted every run, with `OPENMANAGE` now
writing a `reachable=False` placeholder for a rejected login as well as a
dead one; the inventory table gained a one-click per-row maintenance
switch; and UNKNOWN stopped counting as a health verdict (ADR-0027 —
this was a real fleet-wide false CRITICAL on Cisco, not a hypothetical).
See the "Key technical facts" entries for the last two.

**Work before that, 2026-09-10** — cluster membership, finished and
documented. `Server.openshift` is now written by two real CronJobs
(`docs/adr/0024-openshift-cluster-membership.md`), `OpenShiftLifecycle`
was trimmed from ten fields to five at the user's direction,
`OpenShiftState` narrowed to AVAILABLE / INSTALLED /
INSTALLED_TO_INVENTORY, and the kustomize tree at `cronjobs/` was replaced
by a Helm chart at `deploy/helm/openshift-membership` (one release per
cluster, for ArgoCD). Three UI changes landed with it: search now finds
mid-name fragments (`docs/adr/0025-...`), the sites landing page gained
Available/Installed cards, and `?site_id=unassigned` works — it never had,
so the site overview's own Unassigned card had always linked to an empty
list.

That commit also fixed the five CI failures the previous one shipped red,
plus a real runtime bug `ty` caught only because CI never got that far:
`AuditService(...)` called positionally against a keyword-only parameter,
which would have raised `TypeError` on every real collector run.

**Convention 8 is now a CI gate**, not the honor system — see the
convention itself. `scripts/check_comment_density.py` with a baseline of
701 pre-existing violations that may only shrink.

The most recent user direction before that was: real vendor collectors
first, deployment/CD gaps and auth deliberately parked. **Every planned vendor
collector now exists, and as of 2026-09-08 every one of them has had a
live field pass against real hardware, every one finding at least one
real defect** — `UCS_MANAGER`/`UCS_CENTRAL` against UCSPE, `ONEVIEW`
against a live appliance (ADR-0022's "Results, 2026-09-07"), `INTERSIGHT`
against the user's on-prem Private Virtual Appliance, both
`verify_intersight` and `--dry-run` itself (ADR-0017's "second field pass"
section — auth, name resolution, `TotalMemory`'s MiB unit, and five real
defects found and fixed: the `ComputeBoard`-only join gap, a GPU catalog
matcher that couldn't recognize Intersight's own product-name spelling,
and `"OK"` reading UNKNOWN instead of UP/HEALTHY across PSU health,
GPU/NIC `oper_state`, and drive health), and, last to close, `OPENMANAGE`
against a live OME appliance and several iDRAC9 servers on 2026-09-08
(ADR-0020's own flagged "highest-consequence unverified assumption" —
that iDRAC's `SerialNumber` is the Service Tag OME correlates on — turned
out wrong; the real one is `Oem.Dell.DellSystem.NodeID`, fixed the same
day). The natural next steps:

1. **UCS's own leftovers — settled 2026-09-07 by a live UCS Central dry
   run**, see ADR-0009's two "Update (2026-09-07)" sections. **Settled:**
   `total_memory`'s MB assumption is correct (confirmed against the UCS
   UI's own figure, and this also backs Intersight's identical
   assumption); `cpu_model` and per-drive storage detail are confirmed
   populated on real hardware, not just present in the mapping code;
   fabric `fabric_model`/`fabric_serial` are confirmed populated too
   (this was already implemented, just never recorded in the ADR until
   now); and the `health`/`oper=UNKNOWN` question is fully settled —
   `_DISK_HEALTH_MAP` was missing two real failure states (`offline`,
   `self-test-failed`, both now CRITICAL) and `_OPER_STATE_MAP` was
   missing five real `AdaptorExtEthIf` values, both closed against the
   installed `ucsmsdk`'s authoritative enums rather than only what this
   fleet happened to show. Three more raw values were confirmed to be
   correct as UNKNOWN, not gaps: disk `NA`/`unknown` genuinely mean
   "doesn't apply"/"no verdict" in Cisco's own terms, and interface
   `indeterminate` (24% of this fleet's physical ports — common) is
   Cisco's own name for "cannot be determined". A fourth finding wasn't a
   bug at all: `AdaptorHostEthIf.oper_state` (vNICs) turned out to be a
   generic equipment-operability enum, not a link-state one, so reading
   `"unknown"` on 99.75% of vNICs is expected given what the field
   actually measures — no fix exists to make there. **`fabric_name` is
   now built and confirmed live** — `topSystem.name` (the domain's shared
   cluster name; UCS Manager has no per-FI hostname), previewed first in
   `verify_ucs_central`'s section 6, then independently confirmed by the
   user running a short `ucsmsdk` script directly against a real
   air-gapped domain before it was wired in. One more domain-singleton
   query per domain (`ucs_manager/provider.py`), threaded through
   `_attachments`'s new `cluster_name` param. See ADR-0009's second
   "Update (2026-09-07)". **Still open:** `fabric_id` (no source exists)
   and a fully *associated* service profile (nothing tested has gone past
   `config-failure` for want of a boot policy, vNICs and a UUID pool).

2. **OpenManage's own remaining narrow item**: `Oem.Dell.DellSystem.
   NodeID` is confirmed only on iDRAC9. Worth a quick check on any iDRAC7/8
   hardware this estate still runs — those generations may not carry the
   OEM block the same way, and `mapping._dell_serial` falling through to
   `SerialNumber` there would silently reintroduce the wrong serial for
   that generation only.

3. **The Dell iDRAC GPU VRAM check** (`docs/field-test-checklist.md`
   part 3) — one `curl`, opportunistic, only if a Dell server with a GPU
   fitted is ever to hand. Settles whether the built-in GPU catalog needs
   to carry Dell's own spellings or Redfish's `MemorySummary` already
   covers it.

4. **Intersight's own remaining narrow items**, none blocking: the
   DOWN/CRITICAL counterpart to Intersight's `"OK"` vocabulary
   (`normalize_oper_state` and `_drive_health` both), unconfirmed because
   nothing on the tested tenant has actually failed; boot-optimized
   storage (`FlexUtil`/`FlexFlash`), confirmed real but not implemented;
   and the smaller ADR-0017 UNVERIFIED-list items (CPU-name field, BMC
   address precedence, clock skew, account region). Worth another
   `verify_intersight` pass opportunistically, not a scheduled action.

5. **Then the deployment/CD and auth gaps above**, which are the rest of
   what "production and really run" means for this platform — staleness
   detection first, since it is item 0 of the not-done list and nothing
   else answers "40 hosts have been failing for two weeks". Ask the user
   before assuming this is the next phase; the ordering above is the
   direction they have been steering toward, not a plan they have signed
   off on.

~~Give the Dell collector a seeded shape~~ — **already done, kept here as
a standing caution rather than deleted.** `_UNSEEDED_COLLECTORS` is
empty, `COLLECTOR_TYPES` shapes all five collectors including
`OPENMANAGE`, and `provider_type_for` discriminates Dell by
`server.vendor` rather than by `external_id` prefix. Verified 2026-09-07
(Phase 11 of `docs/notes/2026-09-refactor-plan.md`) — this exact item was
stale once before a session caught it, so double-check before trusting it
a third time.
