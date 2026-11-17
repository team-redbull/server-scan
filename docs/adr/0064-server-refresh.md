# ADR-0064: An admin can fetch one server from its vendor manager now

**Status:** Accepted, implemented 2026-11-17.

## Context

Fresh data for one server meant waiting for its collector (every 6 h) or running the whole collector by hand
from the Jobs page (ADR-0055), which reads every server of that manager. The operator wants a button on the
server page, and an API route, that reads only that server.

## Decision

### `POST /api/v1/servers/{id}/refresh`, admin only

The same `get_one()` + `IngestService.ingest_one` a `/servers/available` live recheck runs (ADR-0032; extracted
as `available_servers.fetch_live`, used by both), for one server by id, from the server page's "Fetch now"
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
- **Timeouts:** a refresh waits as long as the recheck (`recheck.timeouts`, `UCS_CENTRAL` 240 s at most);
  the frontend proxy and the Route already allow `route.timeoutSeconds` (300 s), and the SPA sets none.
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

- `POST /servers/available` is no longer the only endpoint that reaches a vendor manager.
- No stored shape changed: one new `EventType` value, two new error codes. Once a refresh has been used, an
  older API image cannot decode the `SERVER_REFRESHED` events, so do not roll the API back past this release.
