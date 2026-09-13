# Server Inventory Platform

A production-grade, air-gapped bare-metal server inventory platform:
MongoDB-backed inventory, a FastAPI REST API, a React admin UI, Redis
cache, a regex-based classification engine, and a declarative health-
policy engine — with real vendor collectors landing one at a time behind
a stable seam (see "How data actually gets in" below).

**New to this codebase, including a fresh Claude Code session picking
this up?** Read [`CLAUDE.md`](CLAUDE.md) first — it has the current
status, the standing project conventions, and exactly where to continue.
Area-specific traps live beside the code they guard, in
`.claude/rules/` (collectors, storage/queries, frontend), and the
session-by-session history in `docs/notes/session-log.md`.

## Status

Phase 1 is functionally complete except real authentication, which is
deliberately deferred — see `CLAUDE.md`. All four vendor managers now
have a collector, plus a fifth for machines no manager owns. Built so
far, in order:

1. Inventory model, MongoDB/Redis persistence, search/filter/sort/cursor
   pagination, the inventory UI.
2. Classification engine (regex-based, scope + priority resolution).
3. Health policy engine (declarative conditions, `policy_key`
   override/shadowing).
4. Maintenance windows and an append-only audit trail.
5. Classification-rule and health-policy UIs — since made read-only and
   merged into one page; rules and policies ship with the platform, so
   every deployment classifies and scores identically. Policies are
   shown grouped by what they apply to — general ones first, then per
   vendor/collector — and ordered by severity within each group.
6. A 10k/50k-scale performance pass (real index-coverage verification and
   load testing, not just fixture-sized tests).
7. Playwright E2E coverage of the critical admin flows.
8. The first real vendor collector: **Cisco UCS Manager**, deployed as a
   Kubernetes CronJob and validated against a live UCS Platform Emulator.
9. A UI rebuilt around what an operator actually scans for: a per-site
   overview as the landing page, and a three-column server list (name,
   model, state) with everything else on the detail page.
10. The second collector: **standalone Redfish**, for machines no
    aggregator owns — a Cisco CIMC that Intersight cannot manage, an
    iDRAC, a current iLO. One BMC at a time, from an inventory file.
11. **Cisco Intersight**, for the Cisco servers no UCS domain owns — the
    one collector whose cost does not grow with the fleet.
12. **Dell OpenManage**, which splits the job: OME says who exists and
    what each machine is called, each iDRAC says what it is
    (`docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md`).
13. **HPE OneView**, the only source for every HPE server whatever its
    iLO generation (`docs/adr/0022-oneview-only-hpe-collector.md`).
14. **Cluster membership** — two jobs that run *inside* each OpenShift
    cluster and report which servers it is actually using, which is the
    one question no vendor manager can answer
    (`docs/adr/0024-openshift-cluster-membership.md`).
15. **`GET /api/v1/servers/available`** — a read API that replaces a
    BMH-creation flow's own live vendor queries with a Mongo-backed lookup
    plus a live recheck of just the candidates it is about to return
    (`docs/adr/0032-available-server-lookup-api.md`).

The inventory table shows **Name, Installation, MCE, Cluster, Model,
State** and a per-row maintenance switch, and sorts on Name, Model,
Installation, Cluster and MCE. The last two are nullable — plenty of
servers have no cluster — which needed a null-aware keyset cursor to page
over without silently dropping rows
(`docs/adr/0026-nullable-sort-fields.md`). MCE appears as a column only
when a row on the page has one.

The maintenance switch is the one write control in the app, and it lives
only in this table: the tools icon opens a card asking why and pauses the
server, the play icon returns it in one click. The server detail page
shows the current state and reason read-only — it has no control of its
own, so there is exactly one place maintenance is switched from.

**The UI is dark only** and deliberately ignores the viewer's system
theme: it is watched on wall displays in dim rooms, and a light flash on
load is the failure mode being designed out.

Each [GitHub Release](https://github.com/team-redbull/server-scan/releases)
lists what changed in it, generated from the commit subjects that also
decide its version number.
`docs/arc42.md` is the structured architecture overview — goals,
constraints, context, deployment view, quality scenarios, and an honest
risk/technical-debt register. See `docs/architecture.md` for the full
per-slice writeup and `docs/adr/` for the individual design decisions.

## How data actually gets in

This is the one idea worth understanding before anything else: **there is
no single "sync" process.** Each hardware vendor's manager (Cisco UCS
Central, Cisco Intersight, Dell OpenManage Enterprise, and HPE OneView)
gets its own small collector program, and each collector runs as
its own **Kubernetes `CronJob`** — one CronJob per manager *type*, not per
physical manager. On a schedule, a CronJob's pod:

1. Reads that vendor's endpoint and login from configuration — one set
   per manager type (`INVENTORY_UCS_CENTRAL_IP` / `_USERNAME` /
   `_PASSWORD`, and the same shape for Intersight, OneView and OME). No
   `Manager` document to create, no secret volume to mount; in Kubernetes
   these arrive from a `Secret` via `envFrom`. (The UCS Central collector
   needs one thing more, and one thing less: `INVENTORY_UCS_MANAGER_USERNAME` /
   `_PASSWORD` to log into each domain, and no UCS Manager endpoint,
   because Central reports every domain's address itself. Intersight's two
   credential variables are not a login at all: it signs every request
   with an API key, so `_USERNAME` is the API Key ID and `_PASSWORD` is
   that key's PEM private half.)
2. Talks to that vendor's real API and normalizes what it reports into
   the platform's vendor-neutral `ProviderServer` shape
   (`app.domain.ports.provider`).
3. Feeds that through the exact same ingestion pipeline every part of the
   platform already exercises with fake data
   (`app.application.services.ingest.IngestService`) — classify, health-
   evaluate, audit, and upsert into MongoDB, all in one write per server.

### The clusters report what they are using

The vendor collectors above answer "what hardware exists". They cannot
answer "is anything using it" — to UCS, a blade running production and a
blade sitting idle both read `associated`. Only the cluster knows.

So a **second kind of job** runs *inside* each OpenShift cluster and
writes `Server.openshift`:

| Job | Runs on | Reports |
|---|---|---|
| `--source nodes` | every cluster | its own worker nodes |
| `--source agents` | MCE hubs | its Agents, bound to a cluster or not |

They correlate to inventory **by hostname**, and each run reconciles only
the servers already naming *its* cluster — claiming what it sees and
freeing what it does not. Nothing in Kubernetes reports a *removal*, so
that reconcile is the only thing that can ever return a server to
`AVAILABLE`. Scoping it per cluster is what makes it safe: a broken job
in one cluster cannot free another's machines.

Deployed by ArgoCD, one Helm release per cluster, from
`deploy/helm/openshift-membership` — a UPI cluster enables the nodes job,
an MCE hub enables both. `docs/adr/0024-openshift-cluster-membership.md`
has the design; the chart's own README has the values.

This is deliberately kept apart from classification: `installation_type`
is a regex verdict on a hostname (what a machine was *named* to be),
`openshift.lifecycle_state` is a cluster reporting what it actually
holds. **When they disagree, the server is misnamed or misplaced — and
that disagreement is the signal**, so nothing reconciles them.

A server's **site** is not configured *per manager*: it is parsed from
the server's own name (`ocp4-prod-tlv-infra-01` -> site `tlv`), so a
misconfigured manager cannot mislabel everything it collects. A name with
no site token is surfaced as "Unassigned" rather than defaulted — as a
card on the sites overview that appears only while something is actually
in it, unlike a configured site, which shows at zero.

**Which sites exist is one environment variable**, because a site code is
a property of your hostname convention rather than of this code:

```bash
INVENTORY_SITES="nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"
```

`code:Display Name`, comma-separated; the display half is optional.
Changing it renames or adds a site across the API, the site cards, the
inventory filter and the seeded classification rules at once — no code
change, no image rebuild, which matters when the images
have to cross an air gap. In Helm it is `config.sites`, which lands in
the ConfigMap the API *and* every collector read, because a collector
derives each server's site at ingest and the two halves must agree. A
malformed value fails at startup rather than producing a site no server
could ever match. See `docs/adr/0018-sites-from-configuration.md`.

**Not every machine has a manager, and those are collected too.** The
`REDFISH_STANDALONE` collector reaches a BMC directly over DMTF Redfish —
any conformant one, including a Cisco server whose CIMC works but which
Intersight cannot yet manage. It is the one collector whose configuration
differs, and it differs because it has to: there is no aggregator to ask
what exists, so **the fleet comes from an inventory file the platform
owns**, and each BMC may have its own login. That file is also the only
collection filter, since a BMC does not know the server's `ocp4-...`
name. Two consequences worth knowing before deploying it: its cost is per
*server* rather than per manager, and a run where some hosts do not
answer is the normal outcome rather than a failure. See
`docs/adr/0016-redfish-standalone-collector.md` and
`docs/test-redfish-standalone-collector.md`.

Which collector produced a record is carried by `source_provider`, not by
the vendor — a Dell reached at its own BMC is still `vendor: dell`. So
`?source_provider=REDFISH_STANDALONE` answers "these have no manager,
don't look for them in OpenManage or UCS", and
`?vendor=cisco&source_provider=REDFISH_STANDALONE` answers "Cisco
standalone".

```
 UCS Central CronJob ─┐   (one login per UCS Manager domain)
 Redfish CronJob ─────┼── (one login per BMC, from an inventory file)
 OpenManage CronJob ──┼──▶  ProviderServer  ──▶  IngestService  ──▶  MongoDB
 Intersight CronJob ──┤       (vendor-neutral)   (classify, health,        ▲
 OneView CronJob ─────┘                           audit, upsert)           │
                                                                           │
 openshift-nodes  CronJob ─┐  in EVERY cluster    writes Server.openshift  │
 openshift-agents CronJob ─┴─ on an MCE hub       only, never IngestService┘
                                                                            │
                                                                            ▼
                                                       FastAPI REST API (reads MongoDB,
                                                       Redis cache-aside on top)
                                                                            │
                                                                            ▼
                                                       React admin UI (inventory table,
                                                       server detail, read-only rules/
                                                       policies page — dark theme only)
```

MongoDB is the only thing that ties a collector run to what the UI shows
— a collector never talks to the API. **The one exception, since
ADR-0032:** `GET /api/v1/servers/available` (for a BMH-creation caller
that needs a server's current state, not last night's) live-rechecks a
handful of already-Mongo-selected candidates by calling the same
`ServerInventoryProvider.get_one()` a collector's bulk pass would, and
persists the result through the same `IngestService` — the API pod holds
the same manager credentials the CronJobs do for exactly this. Every
other endpoint is still MongoDB-only. Adding a new vendor is: write a `ServerInventoryProvider`
implementation for it (see `app.infrastructure.providers.ucs_manager` as
the reference), register it in `tools/run_collector.py`, and add a
CronJob to `deploy/helm/server-scan` — nothing in the API, the
classification engine, the health engine, or the frontend needs to
change. (The membership jobs are the other chart,
`deploy/helm/openshift-membership`, and do not go through this seam at
all — they write `Server.openshift` directly.) `docs/adr/0009-ucs-
manager-collector.md` is the detailed writeup of how the first provider
was built and validated, and `docs/adr/0014-ucs-central-multi-domain-
collector.md` of how the Cisco collector drives it once per domain.

Five collectors exist today: `UCS_CENTRAL`, `INTERSIGHT`, `OPENMANAGE`,
`ONEVIEW` and `REDFISH_STANDALONE`.

**`INTERSIGHT` is the only one that reaches this platform's 10,000-server
ceiling without qualification.** Every child object in Intersight's model
carries a reference back to its owner, so each sub-resource is listed
once for the whole estate and joined in memory — one run costs on the
order of a hundred requests whether the tenant holds fifty servers or ten
thousand, against the Redfish collector's ~25 *per BMC*. It excludes
servers reporting `ManagementMode == UCSM` by default, because those are
exactly the ones `UCS_CENTRAL` already collects; the two partition the
Cisco fleet rather than fighting over it. **It was validated against the
user's own on-prem Private Virtual Appliance on 2026-09-07**, which found
and fixed a GPU-catalog matching bug and two health-status fields that
were silently reading as unknown — see
`docs/adr/0017-intersight-collector.md`'s "A second field pass
(2026-09-07)" section. An air-gapped site needs an on-prem Private
Virtual Appliance; it cannot reach `intersight.com`.

`UCS_CENTRAL` covers the UCS-managed Cisco fleet. It asks UCS Central which domains are registered and what
their addresses are, then reads each domain's inventory live from that
domain's own UCS Manager — the data path validated end to end against a
live Cisco UCS Platform Emulator, and, as of 2026-09-07, against a real
air-gapped UCS Central domain too (see `docs/adr/0009`'s validation
sections, including the three dated "Update (2026-09-07)" entries).
Central supplies the domain list and the service-profile
names; the servers themselves come from the domains. `docs/adr/0014`
covers the design, its costs, and what is still unproven — including that
a domain not registered with Central cannot be collected at all.

`OPENMANAGE` collects Dell, and is the one collector that reads from two
places on purpose: two bulk calls to the OpenManage Enterprise appliance
say which servers exist and what the operator named them, and each
server's own iDRAC is then read over Redfish for the hardware, because
only the BMC reports measured values. It therefore needs two logins —
`INVENTORY_OME_USERNAME`/`_PASSWORD` for the appliance and
`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` for a shared read-only iDRAC
account — and egress to the whole BMC network. **It is now the only
collector with no live-hardware pass of its own** — every other vendor
collector has had one — though its hardware half reuses the Redfish
mapping and so inherits `REDFISH_STANDALONE`'s validation for that part.
See `docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md`.

`ONEVIEW` collects HPE, and deliberately does **not** copy that split.
The estate runs iLO 4, 5 and 6 in the same racks, and iLO 4 predates
useful Redfish coverage, so a split design would mean a per-generation
branch and two different sets of field provenance for one vendor. Instead
**OneView is the only source for every HPE server, whatever its iLO
generation**: no Redfish pass, no BMC credentials, no branch. What
OneView cannot report for an older machine is reported as `None` — "not
read this run" — never as zero. Three bulk calls cover the whole
appliance, because `GET /rest/server-hardware` returns the complete
object per member and `expand=all` folds in each server's DIMMs, drives,
GPUs and PCI devices. **It was validated against a live appliance on
2026-09-07** (821 servers) — the run found and fixed a real
storage-mapping bug the same day. See
`docs/adr/0022-oneview-only-hpe-collector.md`'s "Results, 2026-09-07"
section and `docs/hpe-collectors.md`.

`UCS_MANAGER` is now the only manager type with no entry point of its
own, and that is deliberate rather than missing: UCS Manager is reached
through `UCS_CENTRAL`, which discovers every domain's address at runtime
and logs into each one. `tools/run_collector.py` says so in as many words
rather than reporting an unimplemented feature.

### Running the collector by hand

It needs five values: where UCS Central is and how to log in, plus a UCS
Manager login that works on every domain. There is deliberately no UCS
Manager endpoint — Central reports each domain's address itself. Set them
in `.env` (see `.env.example`) or inline:

```bash
export INVENTORY_UCS_CENTRAL_IP=ucsc.example.com   # bare host, never a URL
export INVENTORY_UCS_CENTRAL_USERNAME=inventory-svc
export INVENTORY_UCS_CENTRAL_PASSWORD=...
export INVENTORY_UCS_MANAGER_USERNAME=inventory-svc  # used on every domain
export INVENTORY_UCS_MANAGER_PASSWORD=...

# See what the fleet reports, without writing anything at all:
uv run python -m tools.run_collector --manager-type UCS_CENTRAL --dry-run

# ...one server only, plus every XML request/response on the wire:
uv run python -m tools.run_collector --manager-type UCS_CENTRAL \
  --dry-run --limit 1 --debug-xml

# The real thing — classify, health-evaluate, audit and upsert:
uv run python -m tools.run_collector --manager-type UCS_CENTRAL
```

A vendor with any of its required values missing is rejected as a
configuration error naming exactly what to set, rather than attempted as
a login that fails as "bad credentials".

`--dry-run` prints the `ProviderServer` each manager reports *before* the
ingestion pipeline reshapes it, including which site each name resolves
to — so a naming problem is visible without a write. `--debug-xml` turns
on `ucsmsdk`'s own request/response dump (passwords are masked by the
SDK); it is very verbose, so pair it with `--limit`.

Intersight takes an API key rather than a login, and has its own
read-only pre-flight:

```bash
export INVENTORY_INTERSIGHT_IP=intersight.com          # or an appliance FQDN
export INVENTORY_INTERSIGHT_API_KEY_ID='<API Key ID>'   # there is no username
export INVENTORY_INTERSIGHT_API_KEY_PEM="$(cat ~/intersight-key.pem)"

# Signs one GET, reports what the tenant holds, and checks TotalMemory
# against the sum of a real server's DIMMs. Writes nothing:
uv run python -m tools.verify_intersight

uv run python -m tools.run_collector --manager-type INTERSIGHT --dry-run
uv run python -m tools.run_collector --manager-type INTERSIGHT
```

Run the verifier first — it settles the one assumption that would
otherwise silently mis-report every server's memory. This has already
been checked once, against the user's own on-prem Private Virtual
Appliance on 2026-09-07 (see `docs/adr/0017-intersight-collector.md`),
but run it again against your own tenant before trusting it in
production. `docs/field-test-checklist.md` is the short version — the
four variables and the one command, plus what to send back; and
`docs/test-intersight-collector.md` is the full runbook.

OneView has the same shape of pre-flight, and was validated the same way
— against the user's own live appliance, on 2026-09-07:

```bash
export INVENTORY_ONEVIEW_IP=oneview.example.com   # bare host, never a URL
export INVENTORY_ONEVIEW_USERNAME=inventory-svc
export INVENTORY_ONEVIEW_PASSWORD=...

# Logs in, answers the ten questions the docs could not, logs out.
# Writes nothing — no MongoDB connection, no ingest:
uv run python -m tools.verify_oneview

uv run python -m tools.run_collector --manager-type ONEVIEW --dry-run
uv run python -m tools.run_collector --manager-type ONEVIEW
```

Its headline check is whether `processorCount * processorCoreCount` equals
the core count summed from `/processors` — HPE documents
`processorCoreCount` as *per processor*, and reading it through
unmultiplied would halve every two-socket server's core count silently.
`docs/field-test-checklist.md` covers this run too.

## Local development

Requires [uv](https://docs.astral.sh/uv/), Node 24+, and a container
runtime (Podman or Docker) for the dev Mongo/Redis stack.

```bash
# 1. Bring up MongoDB + Redis. Either of these works:
docker compose up -d mongo redis   # or: podman-compose up -d mongo redis
scripts/dev-up.sh up               # no compose provider required

# 2. Backend
uv sync --all-groups
cp .env.example .env          # adjust if needed
uv run uvicorn app.main:app --reload --port 8080 --app-dir backend

# 3. Seed realistic fake data (through the real ingestion pipeline —
#    not a shortcut that writes documents directly):
uv run python -m tools.seed_inventory --count 1000 --seed 42

# 4. Frontend
cd frontend && npm install && npm run dev
```

Then open http://localhost:5173 for the inventory UI, or
http://localhost:8080/docs for the API's OpenAPI docs.

When you are done, stop the UI and API processes and bring the stack down
the same way you brought it up (`docker compose down`, `podman-compose
down`, or `scripts/dev-up.sh down`). The three name their containers
differently but all bind 27017 and 6379, so a stack left up one way is
invisible to another way's `ps` and still holds the ports.

Note that `podman compose` (with a space) is **not** `podman-compose`
(with a hyphen) and does not work here — it needs a socket that systemd
would normally start. Use the hyphenated one, or Docker.

### Fake data

`tools/seed_inventory.py` is the only way to get a fleet without vendor
hardware. It runs the *real* ingestion pipeline — the same
`ProviderServer` -> classify -> health-evaluate -> audit -> upsert path a
collector drives — so seeded data exercises what production does rather
than a shortcut that writes documents directly.

```bash
uv run python -m tools.seed_inventory --count 1000 --seed 42
```

`--count` defaults to 1000 and `--seed` to 42; the same pair always
produces the same fleet, field for field.

What you get mirrors the collectors that exist. Cisco blades arrive
as `source_provider=UCS_CENTRAL` with Central-rooted DNs, service-profile
org paths and fabric attachments; Cisco rack units arrive as
`INTERSIGHT` with `intersight/<moid>` ids and no org path; HPE ProLiants
arrive as `ONEVIEW` with `/rest/server-hardware/<uuid>` ids; Dell servers
arrive as `OPENMANAGE`; and genuinely standalone whiteboxes arrive as
`REDFISH_STANDALONE` with `redfish://` addresses. All five collectors are
seeded and all five are filterable from the UI's Source filter. Each collector's
*absences* are reproduced too, because a fixture richer than the real
thing hides the gaps worth seeing. Names span the estate's real shapes,
including a deliberate minority carrying no site token, so "Unassigned"
is reachable.

**What to look at once it's seeded** (`--count 1000 --seed 42`):

* **The sites overview's six fleet cards** read 1000 across all sites,
  585 UPI, 313 hosted-cluster and 102 MCE on the first row, then 204
  available and 796 installed on the second. Measured, not estimated —
  re-measure rather than trust these if the generator changes. Nothing
  lands in `UNCLASSIFIED`, which is not a seeding gap: UPI's default rule
  became an unconditional `.*` on 2026-09-10, so the system rules cannot
  produce that state at all. Every card is meant to be non-empty and
  visibly different — an available server is a real state, not a seeding
  accident.
* **The four site cards plus Unassigned**: 211 nyc, 224 tlv, 224 bat-yam,
  220 five, and 121 with no site token in the name. That last one is why
  the Unassigned card is worth having — and why it is hidden when it
  would read zero, since on a real estate it usually does.
* **Both network link policies fire, and Cisco is deliberately exempt.**
  22 servers report every readable link down (CRITICAL) and 17 report one
  up (MAJOR), while the 324 UCS/Intersight servers report `UNKNOWN` for
  every vNIC and are scored on nothing —
  `docs/adr/0027-unknown-is-not-a-reading.md`. Before that ADR those 324
  were all CRITICAL, which is what made the two real cases invisible.
* **Availability is not derived from the name.** A seeded server named
  `ocp4-prod-tlv-compute-01` can come back `AVAILABLE`, because a freed
  server keeps the name it was installed under. That disagreement between
  what a name claims and what a cluster reports is the whole point of
  keeping the two apart (ADR-0024) — if only `random-server-*` entries
  were ever free, the Available card would be lying about the shape of a
  real fleet.
* **GPU VRAM comes from the catalog, not from the fixture.** Only
  Redfish-sourced collectors can read a GPU's memory; Cisco and HPE have
  no field for it (see
  `docs/adr/0021-built-in-gpu-catalog-with-model-matching.md`), and the
  fake provider models the catalog path: every generated GPU carries
  `memory_bytes=None` and whatever the UI shows was filled in at ingest
  by `GpuCatalog`. Cisco-collected servers report a PID
  (`UCSC-GPU-L40S`), Dell/HPE ones the vendor's model string
  (`NVIDIA A100-PCIE-40GB`, `AMD Instinct MI300X`), and both match. A
  minority deliberately carry a card the built-in table does not
  answer for — a PID it has never been taught (`UCSC-GPU-A100-40`), a
  genuinely ambiguous `NVIDIA A100` (the A100 shipped in 40GB *and*
  80GB), an absent `NVIDIA RTX A6000` — and those show the raw
  identifier with no VRAM. That is the state `INVENTORY_GPU_MODELS`
  exists to close, and it has to be visible here rather than discovered
  in production.
* **HPE spans three ProLiant generations.** Gen11 and Gen10 inventory
  fully; Gen9 carries an iLO 4, against which every OneView subresource
  call fails, so those servers come back as identity-only records — the
  provider reports `None` for drives, GPUs and NICs, never zero or an
  empty list. (The stored document still shows `0`/`[]` on a *first*
  ingest: `Hardware` has no "unknown" state, and the `None` contract's
  job is to stop a later run overwriting good data — see
  `docs/adr/0016-redfish-standalone-collector.md`.)

**Re-seeding needs an empty database.** Servers correlate on
`(vendor, serial)`, so seeding a different `--count`/`--seed` (or a fleet
generated before a change to the generator) over an existing one reports
errors rather than replacing it. Wipe first:

```bash
scripts/dev-up.sh down && scripts/dev-up.sh up
uv run python -m tools.seed_inventory --count 1000 --seed 42
```

### Tests

```bash
uv run pytest                     # backend: unit + integration + api tests
uv run ruff check .               # lint
uv run ruff format --check .      # formatting
uv run ty check backend/app tools tests # type check
uv run lint-imports                # layering (pyproject.toml's [tool.importlinter])

cd frontend
npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                  # Playwright — needs the dev stack + backend + frontend all running
```

Docstring coverage against CLAUDE.md's convention 8 is a real CI gate
now (`D` is part of `uv run ruff check .` above, per
`docs/notes/2026-09-refactor-plan.md` Phase 10), not a local-only check.

Not CI gates, but worth running locally when touching a lot of code at
once: `uvx vulture backend/app tools --min-confidence 80` (dead code —
expect two known false positives, `__aexit__`'s unused `exc_type`/`tb`)
and `npx knip` from `frontend/` (unused exports — currently the ~450 LOC
left behind by the removed rule/policy editors, tracked as its own
cleanup).

The integration tests need the dev stack; without it they skip rather
than fail, and the whole directory reports `60 skipped` in about five
seconds. If they take noticeably longer than that, something is wrong
that this note does not cover.

At scale, `tools/verify_indexes.py` and `tools/loadtest.py` verify query
plans and latency against a real 10k/50k-server seeded dataset — see
`docs/adr/0007-scale-verification-and-request-coalescing.md`.

## Container images

Every push to `main` that passes the full test suite builds and
publishes both images (API/collectors, and the frontend) to GHCR —
`ghcr.io/team-redbull/server-scan-api` and
`ghcr.io/team-redbull/server-scan-frontend` — tagged with a semantic
version decided automatically from
[Conventional Commits](https://www.conventionalcommits.org/) since the
last release (`feat:` → minor, `feat!:`/a `BREAKING CHANGE:` footer →
major, anything else → patch), plus rolling `latest` and `sha-<commit>`
tags. Note the patch number advances once per *commit*, not once per
push, so a push of three `fix:` commits moves it by three. See
`docs/adr/0010-image-publishing-and-versioning.md` for the full design
and `deploy/` for the manifests that consume these images.

Every action in the workflow is pinned to a commit SHA rather than a
tag, and none of it updates itself — `docs/adr/0013` explains why, and
`CLAUDE.md`'s "Keeping CI current" is the periodic pass that keeps it
from going stale.

## Project layout

```
backend/app/     FastAPI service — domain/application/infrastructure/api layers
frontend/        Vite + React + TypeScript SPA
tests/           unit / integration / api tests
tools/           operational CLIs: fake-data seeder, index/load verification,
                 the real-collector runner (tools/run_collector.py)
scripts/         dev environment helpers
deploy/          Helm charts: server-scan (API, frontend, per-vendor
                 collector CronJobs) and openshift-membership (the two
                 per-cluster jobs that report what each cluster is using)
docs/            architecture notes, ADRs, cisco-collectors.md (the
                 verified implementation facts the Cisco collectors rest on)
                 and test-ucs-collector.md (runbook for proving the
                 collector against real hardware)
```
