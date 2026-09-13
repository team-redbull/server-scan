# ADR-0007: Query plans are verified against real 10k/50k data, not fixture-sized data; list reads are request-coalesced

## Status

Accepted

## Context

The platform's stated performance target is ~10,000 servers with headroom
to 50,000+ (`docs/architecture.md`'s "Purpose" section). Every index in
`app.infrastructure.mongodb.indexes` was designed with that scale in mind
and spot-checked with `.explain()` against small integration-test fixtures
(dozens of documents). That's not the same thing as verifying it: MongoDB's
query planner is a cost-based optimizer, and at a few dozen documents it
will happily choose a full collection scan over a barely-selective index
because the scan is *actually cheaper* at that size — a fixture-sized
`.explain()` assertion can pass for a query shape that silently regresses
to a collection scan once the collection is large enough that the index
would have mattered.

`tools/seed_inventory.py` was timed and found to scale linearly (~5.5ms/
server end-to-end, including classification and health evaluation) —
50,000 servers seed in well under 5 minutes — so seeding real-scale data
for verification is cheap enough to do routinely, not just once. Two new
tools do the actual verification:

- `tools/verify_indexes.py` runs `.explain()` for every query shape
  `GET /api/v1/servers` (and the classification/health resolution and
  audit-event paths) can issue, against whatever is currently seeded, and
  fails loudly if a shape that has a supporting index falls back to an
  unexpected `COLLSCAN` anyway.
- `tools/loadtest.py` measures real p50/p95/p99 latency for the same
  shapes under concurrent load against a running API instance — plan
  shape alone doesn't say whether 6,000 examined keys is 5ms or 500ms on
  real hardware, or what happens when many callers hit the same expensive
  shape at once.

Run against a genuine 50,000-document `servers` collection, both tools
found real problems that fixture-sized tests had not (and structurally
could not have) caught:

### 1. `last_seen_at`'s index was missing its `_id` tiebreak

Every other unfiltered-sort index in `SERVER_INDEXES` — `name_id`,
`serial_id`, `model_id`, `updated_at_id` — is `(sort_field, _id)`, matching
the pattern keyset pagination needs (see `indexes.py`'s module docstring).
`last_seen_at`'s index shipped as `(last_seen_at)` alone. MongoDB cannot
use a single-field index to satisfy a `.sort([(last_seen_at, 1), (_id,
1)])` — the compound sort key isn't a prefix of what the index provides —
so an unfiltered `sort=last_seen_at` request fell back to a full
`COLLSCAN` plus a blocking in-memory sort. Fixed by replacing it with
`last_seen_at_id: (last_seen_at, _id)`.

### 2. `maintenance.enabled` — a whitelisted filter with no compound index

`app.domain.services.search.FILTER_FIELDS` whitelists `maintenance` (→
`maintenance.enabled`), and `indexes.py`'s own module docstring states the
design rule: "One compound index per filter whitelisted in
`FILTER_FIELDS`, each ending in the default sort field + `_id`." No such
index existed for `maintenance.enabled` — an oversight relative to the
module's own stated rule, not a deliberate omission. Filtering by
maintenance state (combined with the `last_seen_at` bug above, on that one
sort field specifically) fell back to a full `COLLSCAN`. Fixed by adding
`maintenance_enabled_name_id: (maintenance.enabled, name_normalized,
_id)`, matching every sibling filter index's shape.

Both were caught purely by running the exact same `.explain()` assertions
against 50,000 real documents instead of a test fixture; the query code
itself never changed, only the two index definitions.

### 3. Cache-stampede tail latency on `GET /api/v1/servers`

`tools/loadtest.py`, run at 20 concurrent callers against a low-selectivity
search term (`search=ocp-dell`, matching roughly a quarter of the 50k
fleet), showed a p50 of 30ms but a p95/p99 in the **4-second** range.
`app.infrastructure.redis.cache.CacheClient` is a plain cache-aside with no
deduplication: when many identical requests arrive within the same
15-second `LIST_PAGE_TTL_SECONDS` window before the first one has written
its result back, *every one of them* independently misses the cache and
independently re-runs the same multi-thousand-document Mongo scan — the
textbook cache-stampede / dogpile-effect failure mode. This is a realistic
scenario, not a synthetic one: a saved dashboard filter or a shared link
that many browser tabs/users poll at once produces exactly this request
pattern.

## Decision

Add in-process request coalescing (`app.infrastructure.singleflight.
coalesce`) around the cache-miss path in `GET /api/v1/servers`: concurrent
callers requesting the same cache key while a computation is already in
flight `await` that same in-flight `asyncio.Future` instead of issuing
their own redundant Mongo query. Deliberately scoped to a single process
(a plain `dict[str, asyncio.Future]`, no distributed/Redis lock) — a load
balancer already spreads concurrent requests for the same resource across
replicas, so per-process coalescing removes most of the duplicate work
without taking on a distributed lock's own contention and staleness
failure modes, and Phase 1 has no multi-worker deployment yet to even
measure cross-process contention against.

Re-running the same load-test scenario after the fix: p99 dropped from
~4,057ms to ~156ms — Mongo now does the expensive scan once per stampede
instead of once per concurrent caller.

## A related, deliberately undecided finding

The same load test surfaced a second, arguably more important tail-latency
case: a search term that matches **zero or very few** documents (e.g. a
mistyped hostname, or a search-as-you-type request sent before the user
has finished typing) cannot benefit from `list_page`'s early-stop
`limit(page_size + 1)` — with no matches to accumulate, the query must
examine the *entire* collection before it can conclude there's nothing
more to find. Measured at 50k: p99 ≈ 700-800ms for a non-matching search,
reproducible across repeated runs (not noise). Request coalescing doesn't
help here, since a mistyped or partially-typed search is rarely the exact
same string across concurrent callers.

This is a real, quantified characteristic, not a bug in the code added
this slice — it's inherent to `list_page`'s existing plan-selection
behavior (the sort field's index drives the scan; the search filter is
applied as an in-memory `FETCH`-stage regex check, so its cost is
proportional to how many documents must be examined before the sort order
happens to produce enough matches, which is unbounded when there are too
few). A real fix needs a genuine trade-off decision this ADR does not make
lightly: forcing the planner onto the `search_tokens` index (via `.hint()`
or a compound `(search_tokens, name_normalized, _id)` index) bounds the
*filter* cost to the true match count, but MongoDB cannot use a multikey
index to also provide sort order for a non-equality condition on the
array field — so it would trade this problem for a blocking in-memory sort
whose cost scales with match count instead, which is *worse* for a
moderately common search term (thousands of matches) even though it's
much better for the empty/near-empty case this ADR is about. Changing this
touches the same query path `docs/adr/0006`'s cursor-pagination fix
already had to get exactly right; doing it under this slice's time budget
risked the correctness of an already-verified system for an
improvement whose net direction depends on real search-term distribution
this platform doesn't have production data for yet. Left as a documented,
quantified, deliberately open finding for a follow-up slice — not silently
absorbed into "performance pass: done."

## Consequences

- `tools/verify_indexes.py` and `tools/loadtest.py` are now part of the
  toolset for evaluating any future index or query-shape change — re-run
  both against a freshly `tools/seed_inventory.py --count 50000`-seeded
  database before trusting a fixture-sized `.explain()` assertion alone.
- `app.infrastructure.singleflight.coalesce` is a general-purpose,
  dependency-free primitive — any future endpoint with the same
  cache-aside-plus-expensive-recompute shape (not just `GET /servers`)
  should consider it, not just this one call site.
- **Updated 2026-09-06 (Phase 2 of the production-hardening pass):**
  the computation is owned by its own `asyncio.Task`, created once per
  key, rather than run directly inside whichever caller happened to
  arrive first. The original design let a cancelled *waiter* cancel the
  shared `Future` (crashing the leader) and let a cancelled *leader*
  poison the shared `Future` for every waiter — both reachable from one
  disconnecting client, `asyncio.shield()` alone only fixes the first.
  See `backend/app/infrastructure/singleflight.py`'s module docstring for
  the mechanism, and the app lifespan's new `drain()` call, which cancels
  any computation nobody is waiting on any more before Mongo/Redis close.
- The zero/near-zero-match search tail latency is accepted, quantified,
  and documented, not fixed, in this slice. It should be revisited once
  there's real production search-term distribution to reason about the
  hint-vs-current trade-off with, or if it starts showing up as a real
  operational complaint rather than a benchmark artifact.
- **Updated 2026-09-06/2026-09-07 (Phase 5 research, Phase 11 of the
  production-hardening pass): the named mechanism is confirmed, but the
  numbers above do not reproduce under the current seed** — this is a
  measurement caveat, not a retraction. Re-measured with `explain()`
  against both a 10k and a 50k `--seed 42` dataset
  (`docs/notes/2026-09-research-performance.md` §7.3):
  - **Confirmed**: at 50k, a low-selectivity search (`^ocp-dell`, 1,570
    matches) plans as `IXSCAN[name_id]` with the regex applied as a
    `FETCH`-stage filter, examining 1,604 documents to return 51 — the
    exact mechanism described above. At 10k the planner instead chose
    `search_tokens` for the same query. MongoDB really does flip plans
    with scale, exactly as this ADR predicted a `search_tokens` hint
    would force it to.
  - **Did not reproduce**: the zero-match case is the *cheapest* query in
    the set at both scales (0-1 keys, 0 documents, ≤1 ms), because the
    planner already picks `search_tokens` for it — nothing approaches the
    700-800 ms p99 measured originally. `--seed 42` puts 1,570 servers
    (3% of the fleet) behind the low-selectivity term above, not the
    "roughly a quarter" this ADR's original scenario describes — a
    different seed distribution, not a different mechanism, and not
    directly comparable to the original p99s either (those were measured
    under 20 concurrent callers post-coalescing; the re-measurement is
    single-query `explain()` plans).
  - The action remains what it was: revisit if this shows up as a real
    operational complaint, with production search-term distribution to
    reason about the hint trade-off against. Nothing here changes the
    coalescing decision this ADR is actually about.

## Update (2026-09-13): three `singleflight` details, moved out of the code

Recorded from `app.infrastructure.singleflight`'s inline comments; the
module docstring keeps the two cancellation failure modes the 2026-09-06
update above already points at.

- **The done-callback is registered *after* `_inflight[key] = task`, not
  in `_run`'s own `finally`.** Under `asyncio.eager_task_factory` (3.12+;
  not enabled by this project today, but a real risk if it ever is),
  task execution starts synchronously inside `create_task()`, so
  `compute()` can finish before `create_task()` returns. A `finally`
  inside `_run` would then run *before* the insertion line, find nothing
  to clean up, and leave a completed Task cached under that key forever.
  A done-callback added after insertion is scheduled via `call_soon`
  even for an already-finished Task, so it still runs, one tick later —
  verified against this project's own interpreter.
- **`_cleanup` logs an exception nobody was left to observe.** If every
  shielded waiter was cancelled before the task settled, the exception
  would otherwise surface only as asyncio's "exception was never
  retrieved" warning at GC time, with no context at all. Calling
  `task.exception()` already marks it retrieved, so the
  `singleflight.unretrieved_exception` warning is purely additive.
- **`drain()` runs before the Mongo/Redis clients close, not after.**
  `coalesce()` deliberately detaches a computation from every caller
  that stops waiting on it — so one disconnecting client cannot fail the
  others — which means a computation can outlive every request that
  ever cared about it. `asyncio.run()`'s own cancellation of remaining
  tasks happens *after* the lifespan's `finally` has run, so left to
  that, such a computation would be mid-query against a client that no
  longer exists. Hence the explicit cancel-and-gather in `lifespan`.
