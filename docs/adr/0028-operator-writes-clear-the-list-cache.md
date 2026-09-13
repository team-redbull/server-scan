# ADR-0028: an operator write clears the list cache; an ingest write does not

Date: 2026-09-12
Status: Accepted

Amends `docs/adr/0007-scale-verification-and-request-coalescing.md`, which
introduced the cache-aside list page and its TTL.

## Context

`GET /api/v1/servers` is cache-aside under `list_key(filter_hash,
cursor_hash)` with a 15-second TTL, and `GET /api/v1/servers/facets` under
`facets_key(filter_hash)` with 60. **Neither has ever been invalidated on
write.** That was a deliberate, documented decision and it is still right
for the case it was made for: five collector CronJobs write continuously
across a 10,000-server fleet, and clearing every cached page on every
ingest would leave the cache permanently cold, which is the opposite of
what it is for. A page that is at most 15 seconds stale is a good answer
to "show me the fleet".

It is a bad answer to "I just clicked this". Two things changed that:

1. The inventory list gained a per-row maintenance switch, so the list is
   now a place an operator *writes* from, not only reads.
2. The operator reported the actual symptom: a server taken **out** of
   maintenance kept appearing under the "Maintenance only" filter.

The second is the sharper case and shows why the TTL is not survivable
here. `?maintenance=true` is its own cache key. Its cached page was
computed while the server *was* in maintenance, so after the write the row
is not merely stale — it is in a list whose entire meaning is "these are
in maintenance", and it is not in maintenance. The facet count beside the
filter is wrong at the same time, on a 60-second TTL.

A client-side patch cannot fix that. Rewriting the row in the cached page
flips its icon, but under `?maintenance=true` the row must *leave the
list*, which no in-place edit can express. Refetching cannot fix it
either, as long as the refetch is served the same cached page — which is
exactly what was observed: the write succeeded, the refetch fired, and the
pre-write page came back.

## Decision

**`PUT` and `DELETE /api/v1/servers/{id}/maintenance` clear every cached
list page and facet count. Nothing else does.**

`_invalidate_list_cache` calls `CacheClient.delete_matching` over
`keys.list_and_facets_patterns()` (renamed `list_cache_patterns()` on
2026-09-14, when `/servers/facets` was removed and the `/servers/rows`
body joined the list pages under it — ADR-0033). Three properties are
load-bearing:

- **It is on the operator path only, never on ingest.** `IngestService`
  and every collector keep paying the TTL. This is the whole reason the
  decision is defensible: maintenance writes are human-initiated and rare
  (a handful a day), while ingest is tens of thousands of writes per run.
- **It clears both shapes together.** A row that changed changes the pages
  it appears on *and* the counts it is summed into; invalidating one
  without the other trades a stale row for a stale number.
- **It uses `SCAN`, never `KEYS`.** `KEYS` blocks the Redis server for the
  whole scan, which on a 50k-deployment's keyspace is long enough to stall
  every other request — a fleet-wide outage caused by one operator
  clicking a button.

Two smaller things that are easy to get wrong and are written down because
both were:

- `SCAN MATCH` is glob-style — `*`, `?`, `[]` — with **no brace
  alternation**. A `si:1:{list,facets}:*` pattern matches the literal
  string and therefore nothing at all, silently. Hence two patterns.
- The frontend still patches the row from the write's own response
  *before* refetching, so the icon flips on the same frame as the click
  rather than a round trip later. The refetch behind it is only correct
  because the server clears its cache before responding.

## Alternatives rejected

**Invalidate on every write, ingest included.** The original reason not to
still holds exactly: a collector run would clear the cache continuously
and the hit rate would go to roughly zero during every run — which is
precisely when the UI is most used.

**Shorten the list TTL to ~1s.** Cheaper to implement and it makes the
symptom rarer without removing it; a 1-second window is still a window,
and it multiplies the Mongo load the cache exists to avoid by fifteen for
every reader, to fix a problem only writers have.

**Key list pages on a fleet-wide revision counter**, bumped on any write,
so a stale page becomes unreachable rather than needing deletion — the
same trick `server_key` already uses for a single document. This is the
most elegant option and it is the right one if list invalidation ever has
to cover ingest too. It is not worth its cost now: every write would have
to bump and every read fetch the counter, for a case two endpoints cover
with a scan that runs a few times a day.

## Consequences

- The "Maintenance only" filter is correct the instant a write returns,
  and so is its count. `tests/api/test_maintenance_and_events.py` pins
  both, and both fail if `_invalidate_list_cache` is removed — verified by
  removing it.
- Every cached list page in the deployment is dropped on each maintenance
  toggle, so the next read of any filter pays one Mongo query. At the rate
  these writes actually happen this is not measurable; at a rate where it
  would be, the revision-counter alternative above is the answer.
- Redis being down changes nothing: `delete_matching` degrades to a no-op
  and returns 0, like every other `CacheClient` method, and the read path
  falls through to Mongo anyway.
