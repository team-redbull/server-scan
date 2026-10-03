# Server Scan

An air-gapped inventory of bare-metal servers. It collects what every hardware
manager reports, works out what each machine is for and whether it is healthy,
and serves it through a REST API and an admin UI.

Built for a fleet of ~2,500 servers (up to 5,000), across Dell, Cisco, HPE and
standalone BMCs, in several sites.

## What it does

- **One inventory from every manager.** Cisco UCS Central and Intersight, Dell
  OpenManage, HPE OneView, plus direct Redfish for machines no manager owns.
- **Classifies** each server (UPI, hosted cluster, MCE) with regex rules and
  **scores its health** with declarative policies. Both ship with the platform,
  so every deployment judges the same way.
- **Knows what is free.** Jobs inside each OpenShift cluster report which servers
  they actually use, so "available" means no cluster holds it.
- **Keeps an audit trail** of every health, classification, maintenance and
  reservation change.
- **Answers provisioning questions.** `GET /api/v1/servers/available` returns
  live-verified servers for a BareMetalHost generator.

## Architecture

```
 UCS Central ─┐
 Intersight ──┤   one Kubernetes CronJob           ┌─ FastAPI ── React UI
 OpenManage ──┼── per manager type ──▶ MongoDB ────┤  (Redis cache-aside)
 OneView ─────┤   classify, score,    (source of   └─ /servers/available ─▶ vendor
 Redfish ─────┘   audit, upsert        truth)         (live recheck, only here)
                                          ▲
 nodes-status jobs, one per OpenShift     │
 cluster ─────────────────────────────────┘ writes Server.openshift only
```

MongoDB is the only link between a collector run and what the API serves. Adding a
vendor means one `ServerInventoryProvider` implementation and one CronJob; nothing
else changes.

Deep dives: [`docs/arc42.md`](docs/arc42.md) (structured overview),
[`docs/architecture.md`](docs/architecture.md) (per subsystem), and the
[ADRs](docs/adr/) (every design decision). The UI's Architecture page has
interactive diagrams.

## Quick start

Needs [uv](https://docs.astral.sh/uv/), Node 24+ and Docker or Podman.

```bash
docker compose up -d mongo redis        # or: scripts/dev-up.sh up
uv sync --all-groups && cp .env.example .env
uv run python -m tools.seed_inventory --count 1000 --seed 42   # fake fleet, real ingest path
uv run uvicorn app.main:app --reload --port 8080 --app-dir backend
cd frontend && npm install && npm run dev
```

UI at <http://localhost:5173>, OpenAPI docs at <http://localhost:8080/docs>. Stop
everything with `docker compose down` (or `scripts/dev-up.sh down`) when done.

## Key concepts

- **Site.** Parsed from the server's own hostname (`ocp4-prod-tlv-infra-01` is site
  `tlv`), never configured per manager. A name with no site token is `Unassigned`.
- **Vendor and source.** `vendor` is `dell`, `cisco`, `hp` or `standalone` (a maker the
  platform does not model). `source_provider` is the collector that found it, so a Dell
  reached directly at its BMC is still `vendor: dell`.
- **Installation type.** A regex verdict on the hostname: `UPI`, `HOSTED_CLUSTER`, `MCE` or
  `UNCLASSIFIED`. It says what a machine was *named* to be.
- **OpenShift state.** `INSTALLED` or `AVAILABLE`, reported by the cluster itself. When it
  disagrees with the installation type, the server is misnamed or misplaced, and that
  disagreement is the signal ([ADR-0024](docs/adr/0024-openshift-cluster-membership.md)).
- **Health.** `HEALTHY`, `WARNING`, `MAJOR`, `CRITICAL`, or `UNKNOWN` when nothing could be
  read. `UNKNOWN` is not a verdict. It is scored per category (memory, storage, network,
  connectivity, power, GPU, BMC) and the worst one wins. An unreachable BMC is `CRITICAL`
  ([ADR-0027](docs/adr/0027-unknown-is-not-a-reading.md),
  [ADR-0037](docs/adr/0037-bmc-reachability-health.md)).
- **Stale.** A server no collector has seen for 12 hours (`INVENTORY_STALE_AFTER_SECONDS`).
- **Maintenance and reservation.** An operator can pause a server with a reason (the only
  write in the UI). A caller can take a time-limited install lock on it
  ([ADR-0035](docs/adr/0035-install-reservation-lock.md)).
- **`None` means "not read this run".** A collector never turns a failed read into zero, so
  good stored data is not overwritten ([ADR-0016](docs/adr/0016-redfish-standalone-collector.md)).

## The UI

| Page | Shows |
|---|---|
| **Sites** (`/`) | Per-site cards: totals, health, installation types, available vs installed. |
| **Servers** (`/servers`) | The whole fleet, filtered, sorted and searched in the browser; maintenance switch; sidebar filters by MCE and cluster. Click a row for the detail page: hardware, NICs, BMC link, health reasons, history. |
| **Events** (`/events`) | The audit trail, filterable by type, server, user and time range. |
| **Rules & Policies** (`/rules`) | The read-only classification rules and health policies. |
| **Architecture** (`/architecture`) | Interactive diagrams of the full flow and each collector. |

The UI is dark only, on purpose: it is watched on wall displays in dim rooms.

## Using the API

Interactive docs are at `/docs` on a running instance. A request:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://<host>/api/v1/servers?vendor=dell&site_id=tlv&page_size=200"
```

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/servers` | List with filters, search and cursor paging. |
| `GET /api/v1/servers/rows` | The whole fleet as flat rows (what the UI loads; ETag, so a poll is a 304). |
| `GET /api/v1/servers/{id}` | Full detail for one server. |
| `GET /api/v1/servers/available` | Live-verified assignable servers for BareMetalHost creation. |
| `GET /api/v1/sites` | Per-site and fleet-wide counts. |
| `GET /api/v1/events`, `/servers/{id}/events` | Audit trail. |
| `GET /api/v1/health-policies`, `/classification-rules` | The rules the platform applies. |
| `PUT/DELETE /api/v1/servers/{id}/maintenance`, `POST/DELETE .../reservation` | Writes (admin only). |
| `GET /health/live`, `/health/ready` | Probes. |

Filters on `/servers`: `site_id`, `vendor`, `health_overall`, `installation_type`,
`openshift_state`, `cluster_name`, `source_provider`, `maintenance`, `stale`. Paging is by
cursor: pass the response's `next_cursor` back until `has_more` is false. Errors are
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents, a standard JSON error
shape with a stable `code` field you can branch on.

Claude Code users: the project ships a skill, `.claude/skills/server-scan-api`,
that teaches Claude to query this API; set the base URL and a viewer token in it.

## Authentication

Set `INVENTORY_AUTH_ENABLED=true` and users sign in with their LDAP / Active Directory
account. Access is **group based**, with **per-user lists** as an alternative:

| Role | Can do | Granted by |
|---|---|---|
| `admin` | Everything, including maintenance, reservations and reclassify | `INVENTORY_ADMIN_GROUPS` or `INVENTORY_ADMIN_USERS` |
| `viewer` | Read-only: the UI and every `GET` | `INVENTORY_VIEW_GROUPS` or `INVENTORY_VIEWER_USERS` |

Lists are comma-separated and case-insensitive. A user in both an admin and a viewer
group gets admin, and a login that matches neither is rejected with 403. Machine callers
(such as a BMH generator) send a static bearer token (admin or viewer) instead of logging
in. After 5 wrong passwords for one username the login answers 429 for 15 minutes
(`INVENTORY_LOGIN_MAX_FAILURES`, `INVENTORY_LOGIN_LOCKOUT_SECONDS`).

## Configuration

Everything is an `INVENTORY_*` environment variable; [`.env.example`](.env.example) lists
them all. The ones that matter first:

| Variable | Meaning |
|---|---|
| `INVENTORY_SITES` | Site codes and names, e.g. `tlv:Tel Aviv,nyc:New York`. A server's site is parsed from its hostname ([ADR-0018](docs/adr/0018-sites-from-configuration.md)). |
| `INVENTORY_<MANAGER>_IP` / `_USERNAME` / `_PASSWORD` | One set per manager: `UCS_CENTRAL`, `OME`, `ONEVIEW`. Intersight signs requests instead: `INVENTORY_INTERSIGHT_IP`, `_API_KEY_ID`, `_API_KEY_PEM`. |

Run a collector by hand with `uv run python -m tools.run_collector --manager-type
<TYPE> --dry-run`. Per-vendor setup and field-test steps are in
[`docs/field-test-checklist.md`](docs/field-test-checklist.md).

## Deployment

Every push to `main` that passes CI publishes `ghcr.io/team-redbull/server-scan-api`
and `server-scan-frontend`, versioned from
[Conventional Commits](https://www.conventionalcommits.org/); the release notes are the
commit subjects ([ADR-0010](docs/adr/0010-image-publishing-and-versioning.md)).

Two Helm charts under [`deploy/helm`](deploy/helm): `server-scan` (API, UI, collector
CronJobs) and `nodes-status` (one release per OpenShift cluster). Values and rollout:
[`deploy/README.md`](deploy/README.md). Mirroring images and dependencies into an
air gap: [`docs/air-gap.md`](docs/air-gap.md).

## Development

```bash
uv run pytest                                              # backend tests
uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools tests
cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
```

`/gate` in Claude Code runs the whole CI gate locally. Project conventions, traps and
current status for contributors (human or Claude) are in [`CLAUDE.md`](CLAUDE.md).

## Layout

```
backend/app/   FastAPI service (domain / application / infrastructure / api)
frontend/      React + TypeScript SPA
tools/         collector runner, fake-data seeder, verification and load tools
deploy/        Helm charts and Grafana dashboards
docs/          arc42, architecture, ADRs, per-vendor collector notes
tests/         unit, integration, API tests
```
