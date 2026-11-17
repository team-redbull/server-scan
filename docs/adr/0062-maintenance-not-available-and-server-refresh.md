# ADR-0062: A server in maintenance is not available; an admin can fetch one server now

**Status:** Accepted, implemented 2026-11-17.

## Context

Two operator requests, shipped together because they meet in the same place (what the UI calls
"Available", and the live read `GET /servers/available` already makes):

1. **A server in maintenance still read "Available".** `openshift.lifecycle_state` is a reading the
   membership jobs write (ADR-0024): `AVAILABLE` means no cluster holds the server. Maintenance is an
   operator flag beside it. So a free server in maintenance showed two states at once: the green
   "Available" Installation badge and the maintenance switch on. It was counted on the Sites "Available"
   card (with an "N in maintenance" link under it) and matched `?openshift_state=AVAILABLE`, the filter the
   Claude skill and the MCP tool use to answer "which servers are free". `GET /servers/available` already
   refused it (`MAINTENANCE` reason), so only the read surfaces disagreed.
2. **Fresh data for one server meant waiting for its collector** (every 6 h) or running the whole
   collector by hand from the Jobs page (ADR-0055), which reads every server of that manager. The operator
   wants a button on the server page, and an API route, that reads only that server.

## Decision

### 1. Available means "no cluster holds it, and it is not in maintenance"

Derived on read, never stored: `lifecycle_state` stays the collectors' reading (writing an intent into it
would be reset by the next membership run, the same reason `Reservation` is not a lifecycle state).

- **API filter:** `?openshift_state=AVAILABLE` on `GET /servers` adds `maintenance.enabled != true`
  (`search.build_filter_query`, an `$and` clause, so `?maintenance=true` beside it matches nothing).
  `INSTALLED` and `INSTALLED_TO_INVENTORY` are unchanged: an installed server in maintenance is still installed.
- **`GET /sites`:** `by_openshift_state` gains a fourth slice, `MAINTENANCE` (no cluster holds these, and
  they are in maintenance). `AVAILABLE` no longer counts them, and the slices still sum to the total.
- **UI:** the Installation badge reads "Maintenance" for such a server (table and Overview), and the
  inventory's client-side `openshift_state` filter, its facet count, sorting and the XLSX export use the
  same derived value (`lib/installation.ts`), so the Sites "Available" card and the list it links to agree.
- `GET /servers/available` is unchanged; it already excluded maintenance.

### 2. `POST /api/v1/servers/{id}/refresh`, admin only

The same `get_one()` + `IngestService.ingest_one` a live recheck runs (extracted as
`available_servers.fetch_live`, used by both), for one server by id, from the server page's "Fetch now"
button. Chosen over running a one-server collector Job: no pod start, a synchronous answer, and the API pod
already holds every manager's credentials it may hold (ADR-0060).

- **Every case a recheck degrades on silently is an error here**, because the caller asked for fresh data:
  no `source_provider`, or no connection for that manager type on this process (always true of
  `REDFISH_STANDALONE`, whose per-host credentials stay CronJob-only, and of `KUBEVIRT` on the hub), or
  another manager's server (`NotThisManagersServerError`) is **409 `SERVER_REFRESH_UNSUPPORTED`** naming the
  manager; the manager not finding it is **409 `SERVER_NOT_AT_MANAGER`** (nothing is written or deleted;
  pruning stays `tools.prune_servers`' job); a vendor failure is the same **503** a recheck answers.
- **Admin only** (`require_admin`), like every write; it also costs a vendor-manager session.
- **Audited:** `SERVER_REFRESHED` with the actor, `data.manager_type` and `data.changed` (content changed,
  so a new `revision`). Transitions it causes are audited by ingest as usual.
- **Concurrency:** calls for the same server share one read (`singleflight.coalesce`). The per-manager cap
  on live reads stays parked (CLAUDE.md, "Explicitly NOT done yet"); admin-only keeps the caller set small.
- **Never `IngestService.ingest()`**, so a refresh never restarts an iDRAC (ADR-0050).
- An unchanged server keeps its `revision`/`updated_at` and gets a new `last_seen_at` (ADR-0044); the UI
  says "no changes" when the returned `revision` equals the one it showed.
- The detail pointer and the list caches are invalidated, as for any operator write (ADR-0028).

### Development: `INVENTORY_FAKE_MANAGERS`

With no vendor reachable in dev, every refresh would be a 409. `INVENTORY_FAKE_MANAGERS=seed:count[:epoch]`
(the values given to `tools.seed_inventory`) makes `build_provider_for_manager_type` return the fake fleet's
provider, for the refresh and the `/servers/available` recheck alike. It is honoured **only when
`INVENTORY_ENVIRONMENT=development`**, so tests (which run as `test` and rely on "no credentials → trust the
stored document") and production can never read fake data. A mismatched seed or count answers
`SERVER_NOT_AT_MANAGER`.

## Consequences

- A caller that used `?openshift_state=AVAILABLE` to list free *and* in-maintenance servers must now ask
  for `?maintenance=true` without it. The Sites "Available" card no longer shows an "in maintenance" link;
  those servers are under the card's total only through the new `MAINTENANCE` slice.
- `GET /servers/available` is no longer the only endpoint that reaches a vendor manager.
- No stored shape changed: one new `EventType` value, two new error codes. Once a refresh has been used, an
  older API image cannot decode the `SERVER_REFRESHED` events, so do not roll the API back past this release.
- A `?openshift_state=AVAILABLE` cursor issued before the upgrade fails `CURSOR_INVALID` once (its filter changed);
  the client restarts from page one.
