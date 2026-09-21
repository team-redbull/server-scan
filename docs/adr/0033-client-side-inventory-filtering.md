# ADR-0033: The inventory page filters, sorts and searches in the browser from one polled `GET /servers/rows`

Date: 2026-09-14
Status: Accepted

## Context

Since slice 1 every inventory interaction — a filter, a sort, a page
turn, a keystroke in search — was a request to `GET /servers` (a 50-row
keyset page) plus a request to `GET /servers/facets` (the counts). That
was the right design for the scale the platform was *verified* at:
ADR-0007 measured against 10,000 and 50,000 seeded servers, and at those
sizes a fleet cannot be held in a browser tab.

On 2026-09-14 the operator corrected the real figure: **~2,500 physical
servers today, at most 5,000 within two years**. The 10k/50k datasets
were always the test headroom, not the estate. That reopens a question
the earlier ADRs never had to ask: is the fleet small enough to send to
the browser once and filter there?

The measurements below were taken on 2026-09-14 against 2,504 seeded
servers (`tools.seed_inventory --count 2500 --seed 42`), the dev stack,
and headless Chromium at 1440×900. The harness is a Playwright script
driving the production build through `vite preview`; five runs, medians.

### What the data weighs

| | Measured |
|---|---|
| Stored document, `avgObjSize` | 5.4 KB (13.6 MB collection, 2.6 MB on disk, 1.0 MB indexes) |
| One `ServerSummary` list row on the wire | 1,298 B |
| One `ServerDetail` | 9.1 KB raw, 2.0 KB gzip |
| A flat inventory row (this ADR's `ServerRow`) | ~520 B raw |
| The whole fleet as rows | 2,504 rows: 1.3 MB raw, **187 KB gzip**; ~2.6 MB / ~380 KB at 5,000 |

### What building it costs the API

| Path | 2,504 rows |
|---|---|
| Today's list path scaled to the fleet: `Server.model_validate` on every document, then `ServerSummary.from_server` | **618 ms** (544 ms of it validation) |
| A Mongo projection to the row fields, then a flat Pydantic model | 16.5 ms query + ~11 ms JSON |
| `blake2b` over the body for an ETag | 1.4 ms |

The first row is why `GET /servers` pages at 50: validating the full
domain model per document does not scale to a fleet-sized response. The
second row is why a fleet endpoint is cheap *if and only if* it never
touches `Server`.

### What the browser would have to do

V8, the full 2,504-row fleet:

| Operation | Time |
|---|---|
| `JSON.parse` of the 1.3 MB body | 12.9 ms |
| Filter on vendor + health + site | 0.19 ms |
| Sort on a nullable field (`cluster_name`) then name | 8.5 ms |
| Facet counts over six dimensions | 4.5 ms |
| Substring search over name + model | 0.48 ms |
| Heap held | ~12 MB |

Every operation is under one frame (16 ms). Doubling to 5,000 doubles
the linear ones; nothing crosses a frame except the parse, once per poll.

### What the guidance says

- TanStack Table's own client-side/server-side guide: client-side when
  "the browser can fetch and retain the complete dataset"; the table
  handles tens of thousands of rows; and **never mix** client-side
  filtering with server-side pagination — it silently filters only the
  loaded page.
- AG Grid's row models: the client-side model is the default; the
  server-side model exists for "much larger datasets with millions of
  records". The limit is "browser memory footprint and data transfer
  time".
- API pagination guides agree that a full load gives an immutable
  snapshot, which paging over live-changing data cannot — seven jobs
  write this collection continuously, so rows could shift between pages.

## Decision

The inventory page fetches the whole fleet as flat rows from one new
endpoint and does everything else locally.

**`GET /api/v1/servers/rows`** returns `{items: ServerRow[], generated_at}`:

- `ServerRow` is only what the table renders or searches on — `id`,
  `name`, `vendor`, `model`, `site_id`, `source_provider`,
  `installation_type`, `health` (overall only), `maintenance`
  (`enabled`, `reason`), `openshift_state`, `cluster_name`, `mce_name`,
  `last_seen_at`, `stale`, `reachable`, and for search parity `serial`,
  `bmc_host`, `macs`. It is built from a **Mongo projection**
  (`MongoServerRepository.list_rows`) through `ServerRow.from_doc`,
  never `Server.model_validate` — the 618 ms row above is the whole
  reason this endpoint exists as a separate shape rather than
  `?page_size=5000`.
- The body is cached as wire bytes in Redis for `LIST_PAGE_TTL_SECONDS`
  under the ADR-0028 invalidation (a maintenance write clears it with
  the list pages), served with `Cache-Control: no-cache` and a **weak
  ETag** (`W/"blake2b(body)"` — weak because gzip changes the bytes but
  not the representation, RFC 9110 §8.8.1). A client whose
  `If-None-Match` matches gets a bodiless 304.
- `generated_at` is the newest `updated_at` among the rows, **not the
  build time**: an unchanged fleet must yield byte-identical bodies or
  no poller ever sees a 304. The first implementation stamped `utcnow()`
  and was caught by the benchmark's idle window (two full 187 KB bodies
  in 60 s instead of two 304s); the API test now flushes Redis between
  two reads and asserts the ETag is unchanged.

**The frontend** (`frontend/src/features/inventory/`):

- One `useServerRowsQuery` — TanStack Query, `refetchInterval: 30_000`,
  `refetchIntervalInBackground: true` so an unfocused wall display keeps
  polling. The browser's HTTP cache does the conditional request on its
  own (`no-cache` + ETag); `fetch` sees a 200 with the cached body, and
  structural sharing then keeps every unchanged row's identity, so a
  304 poll re-renders nothing.
- `rows.ts` holds the pure functions — `filterRows`, `searchRows`,
  `sortRows`, `facetCounts`, `paginate` — and the page is `useMemo` glue.
  Search is a case-insensitive **substring** match over name, model,
  serial, BMC host and MACs (colon or bare-hex form). Sort uses
  `Intl.Collator("en", {numeric: true, sensitivity: "base"})` — a natural
  order, `srv-2` before `srv-10` — with nulls last whichever the
  direction (the server's Mongo sort put them first ascending).
- Pagination is client-side, 50 rows a page, a 1-based `page` URL
  parameter replacing `cursor`; every other URL parameter is unchanged so
  existing links keep working. Page numbers give random access, which a
  keyset cursor never could.
- Facet counts describe the filtered set, and a dimension that is itself
  filtered shows no counts. Every option shows its count, `(0)` included,
  sites too (the operator's call, 2026-09-14: an empty option should look
  empty; the old endpoint's "absent, not zero" and its missing site
  counts were artefacts of the Mongo aggregation). The State column sorts
  by severity rank. Filter navigations build the next URL from the live
  `window.location`, not the hook's render-time copy — two changes under
  ~100 ms apart used to drop the first (a pre-existing bug the rapid
  Playwright walk exposed).
- The maintenance switch patches the row in the cached fleet before
  invalidating, so under "Maintenance only" the row leaves at once.

`GET /servers` **stays** for API consumers (`/servers/available` ranks
with the same filter whitelist; scripts page with cursors).
`GET /servers/facets` was **deleted** the same day: nothing called it once
the UI counted locally, and an open endpoint that runs a full-collection
`$group` on demand is not worth keeping for a hypothetical caller. Its
counts were verified equal to the browser's, option by option, before
removal ("What the verification pass found").

## Consequences

### Measured before → after (2,504 servers)

Same fleet, same harness, five-run medians. "Before" is commit `e949cc3`
(API gzip already on); "after" is this ADR's commits. One caveat that
flatters the before column: the harness runs against `localhost`, where
a request round trip is ~1 ms. In production every "before" interaction
crosses the Route, nginx and the API pod twice (list + facets), so the
gap on a real network is wider than this table shows.

| | Before | After |
|---|---|---|
| Cold load to first row (fresh browser context) | 196 ms | 201 ms |
| Bytes to first row | 167 KB | 350 KB (the fleet is 187 KB of it) |
| Vendor filter click → rows updated | 79 ms | 56 ms (30 ms event-to-DOM) |
| Second filter (health) | 66 ms | 23 ms (15 ms event-to-DOM) |
| Clear filters | 65 ms | 65 ms (24 ms event-to-DOM) |
| Next page | 102 ms | 67 ms (23 ms event-to-DOM) |
| Sort by model | 98 ms | 78 ms (29 ms event-to-DOM) |
| Search (both include the 300 ms debounce) | 363 ms | 343 ms |
| API requests in one operator session (load, 2 filters, clear, page, sort, search) | 11 | 2 (rows + sites) |
| Idle wall display, 60 s | 0 requests — and the screen never updates | 2 conditional requests, both 304, **474 bytes** |
| JS heap after the session | 7 MB | 12 MB |

The harness measures input → the harness noticing the new rows, polling
every 5 ms, so it includes paint; the "event-to-DOM" figures in
parentheses are a second, in-page measurement from the input event to
the first `tbody` mutation and are the React cost alone. Every
interaction is now a commit of 50 rows with no network in the path; the
before column's floor was the round trip, the after column's is the
render. Search is dominated by the debounce in both designs; its gain is
qualitative — substring matching.

The one number that went up is bytes on first load, by the size of the
fleet. That is the trade: 187 KB once (then 304s) for zero requests per
interaction.

### Measured at 10,000 and 50,000 — where this design stops being right

The operator asked for the same harness at the API's verified headroom
so the ceiling is a number. Three-run medians, same fleet generator:

| | 2,504 | 10,000 | 50,004 |
|---|---|---|---|
| Fleet body, gzipped | 187 KB | 736 KB | 3.7 MB |
| API cold build of the body | 27 ms | 262 ms | 1,588 ms |
| API cached hit, gzipped on the way out | ~2 ms | 62 ms | 306 ms |
| 304 round trip | ~2 ms | 9 ms | 67 ms |
| Cold load to first row — before / after | 196 / 201 ms | 157 / 296 ms | 161 / **2,393 ms** |
| Vendor filter — before / after | 79 / 56 ms | 73 / 46 ms | 50 / 50 ms |
| Next page — before / after | 102 / 67 ms | 104 / 49 ms | 89 / 59 ms |
| Sort by model — before / after | 98 / 78 ms | 82 / 89 ms | 85 / 145 ms |
| JS heap after a session | 12 MB | 18 MB | 53 MB |

At 2.5k and 10k the browser design wins or ties on every interaction and
costs 5–140 ms more on first load. At 50k it is the wrong design: 2.4 s
to first row, 3.7 MB per changed poll, a 1.6 s cold build the API pays
every 15 s while anyone is watching, and a 300 ms gzip even on a cache
hit. The old 50-row page, by contrast, is 11 ms at every scale. The
crossover is somewhere between 10k and 50k; the operator's ceiling is 5k,
so nothing here is a plan — it is the number the next person needs when
the estate is not what this ADR assumed.

### What the verification pass found

Before committing, the API was hammered (≈330 requests per run at
32-way concurrency over every endpoint, every filter value, 120 random
two-filter combinations, eight sorts walked over three pages, 50
conditional polls, a maintenance round trip; ~5,300 assertions, three
runs, zero failures at the end) and the UI walked through every select
option with the heading count compared to the server's own facet total
(39/39). Two things it caught that review had not:

- **Search parity.** The API's token search indexes vendor, site and
  installation type as well as the visible fields, so `nyc` matched a
  `random-server-0276` *in* site nyc server-side and not client-side.
  `searchRows` now covers the same fields; the parity check is
  "client substring count ≥ server prefix-token count" for every query.
- **The filter row reflowed.** The form was `flex-wrap`, so a wide count
  (`All sites (50004)`) wrapped Health under Search and ticking
  "Maintenance only" moved the checkbox to the row above. The form is now
  two fixed rows — the seven fields share the first and shrink to fit;
  the two toggles own the second — verified pixel-identical unticked,
  ticked, and with every option widened. The toggles also count only
  while ticked (`Maintenance only (3)`), instead of the generic "count
  unless filtered" rule that showed `Stale only (21)` on an option nobody
  had chosen. Filter navigations pass `flushSync: true` so a controlled
  input never waits on a deferred re-render.

### Costs accepted

- **One 187 KB download per changed poll** (~380 KB at 5,000 servers),
  against 16 KB per page before. On the estate's LAN this is nothing; it
  is the reason the fleet is polled at 30 s and not 5 s, and the reason
  the ETag has to be right.
- **A ceiling, measured above.** Fine at 10k, wrong at 50k; the
  operator's horizon is 5,000. The 10k/50k datasets remain what load
  tests use for the *API* (ADR-0007 still holds for `GET /servers`), not
  a target the UI is built for.
- **Two places compute staleness**: `stale` on a row is set by the API at
  build time from `INVENTORY_STALE_AFTER_SECONDS`; the row is at most one
  poll interval behind. The frontend never learns the threshold.
- **Not done, by choice**: row virtualization (`@tanstack/react-virtual`)
  — page-at-a-time keeps the DOM at 50 rows without a new dependency;
  a Web Worker for filtering — 0.2 ms does not need one; SSE/change
  streams instead of polling — a 304 every 30 s is cheaper than a held
  connection per display and needs no Route timeout change.

### Also shipped on the way

- `GET` responses over 1 KB are gzipped by the API (`GZipMiddleware`,
  level 6 — 15.8× in 0.86 ms vs level 9's 16.1× in 1.57 ms on a real
  page). Nothing in the repo compressed JSON before this.
- The frontend image's nginx gzips its static assets (the 494 KB bundle
  ships as 143 KB) and serves `index.html` with `Cache-Control: no-cache`
  so a deploy is picked up on the next load; the hashed assets carry one
  `public, max-age=31536000, immutable` header instead of two.

## Alternatives considered

- **Keep server-side, add `staleTime`.** Removes the refetch when
  bouncing between two filters; keeps every first click a round trip and
  keeps prefix-only search. The lazy fix for a problem that wasn't cost —
  the requests were 1.5 ms — but the operator's ask was responsiveness.
- **`?page_size=5000` on the existing endpoint.** 618 ms per request at
  2.5k, 1.2 s at 5k, through `Server.model_validate`; and 3.4 MB of
  nested summary instead of 1.3 MB of rows. The projection is the point.
- **Server-Sent Events over Mongo change streams.** Real-time, but one
  held connection per wall display, a HAProxy Route timeout annotation,
  and a debounce layer to batch collector writes. The 30 s poll with a
  304 is simpler and the collectors run four times a day.
- **Virtualized infinite scroll.** Nicer for scanning, a new dependency,
  and it interacts badly with the 1440 px width rule and sticky headers
  (a documented TanStack Virtual issue). Page numbers were the smaller
  change and give random access.

## Update (2026-09-21): an MCE / hosted-cluster / UPI sidebar, derived from the rows

The operator asked to filter by MCE and cluster without maintaining a
list. `ServerRow` already carried `mce_name` and `cluster_name`, so this
is `rows.ts` and the page only — no endpoint, no stored field.

- **Three multi-select lists**: MCEs, *hosted* clusters (a cluster that
  has an MCE on any row) and *UPI* clusters (one that has none).
  `clusterFacets(rows, filters)` builds them from the whole fleet, so a
  cluster appears on the next poll and leaves with its last server.
  A UPI list can therefore contain an MCE hub's own cluster (its nodes job
  reports `cluster_name` with no `mce_name`) — correct by that definition.
- **Semantics**: OR within `mce` and within `cluster`; AND with each other
  and with every other filter. Both live in the URL as repeated params
  (`?mce=a&mce=b&cluster=x`); `updateFilters` accepts arrays and
  `toggleMulti` reads the live `window.location`, for the reason above.
- **Counts** describe what ticking the option would show: each list is
  counted over the rows every *other* filter leaves, and options are never
  removed while filtering, so an unreachable pick reads `0` (the same
  `(0)` rule as the selects). This differs on purpose from the selects'
  "a filtered dimension shows no counts": a multi-select is used to add
  values, so it keeps its own counts.
- **An MCE's servers with no cluster** (`INSTALLED_TO_INVENTORY`) count
  under the MCE and under no cluster; they are reachable through the MCE
  list only.
- **Width**: the sidebar is 12 rem and collapsible; the page container
  became `max-w-[1600px]`. Measured with headless Chromium, MCE column
  showing: 0 px table overflow at 1440 px (29 px with a 14 rem sidebar,
  which is why it is 12 rem), 157 px at 1280 px open and 44 px collapsed.
- **Layout**: the same day, Search and the Maintenance / Stale / Duplicate
  toggles moved out of the top filter block into a toolbar directly above
  the table (operator's call — they act on the list beneath them); the six
  selects keep the top row alone. This supersedes the "two fixed rows"
  layout described above.
