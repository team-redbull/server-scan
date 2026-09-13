---
paths:
  - "backend/app/infrastructure/mongodb/**"
  - "backend/app/infrastructure/redis/**"
  - "backend/app/domain/models/**"
  - "backend/app/domain/services/search*.py"
  - "backend/app/domain/services/cursor.py"
  - "backend/app/observability/**"
---

# Stored shape, queries and the cache — read before touching a repository

Loaded only when a storage-side file is open. Each item names its ADR.

- **Every `datetime` is stored as an ISO 8601 string** (`model_dump(mode="json")`),
  never a BSON date. Any range/cursor/`$lt` comparison must render its
  value through `TypeAdapter(datetime).dump_python(mode="json")` first — a
  real `datetime` in `$gt` matches *nothing*, silently (ADR-0006; bit twice,
  the second time an empty page two on `updated_at`/`last_seen_at` sorts).
- **A change correct on a fresh database is not automatically correct on
  an existing one** — three times on one day (a narrowed enum, an index
  rename that left the old unique constraint enforcing, a nullable sort
  field), each green because every test ran against a database the new
  code had just created. **Write the old shape into a real database and
  read it back before shipping.** Mechanisms: a `mode="before"` validator
  for a retired enum value; **renaming or removing an index means adding
  its old name to `indexes.RETIRED_INDEXES`**, or a deployed database keeps
  enforcing it forever (ADR-0026). `PolicyScope`'s old `scope.manager_type`
  key is still folded in the same way — do not remove while any pre-1.1.0
  database exists (ADR-0030).
- **Sorting on a nullable field needs the null-aware cursor** (ADR-0026):
  `{$gt: null}` matches nothing and `{$lt: "abc"}` skips every null while
  the sort orders nulls first — a naive keyset clause drops rows with no
  error. `cluster_name` and `mce_name` today.
- **Search tokens are word-boundary suffixes** (ADR-0025), which is what
  lets an *anchored* `^` regex find `cisco-m6` mid-name. Do not drop the
  anchor: unanchored costs ~650ms per facet query at 52k servers against
  1–26ms, because it scans the whole multikey index. `search_tokens` is
  written by `IngestService`, so a change reaches a document on its next
  collection. `GET /servers/available`'s `?pattern=` is a deliberate,
  bounded exception — a real `$regex` on `name` (ADR-0032).
- **The list cache is invalidated by operator writes and nothing else**
  (ADR-0028): `/servers` pages (15s) and the `/servers/rows` body (15s,
  ADR-0033) get no invalidation on the ingest path, since five CronJobs
  write continuously.
  Maintenance and `GET /servers/available`'s live recheck call
  `_invalidate_list_cache`. **Do not extend this to ingest.** `SCAN MATCH`
  has no brace alternation (`{a,b}` matches nothing), and it must
  never become `KEYS`.
- **Redis is cache-aside only**; every read degrades to Mongo on any Redis
  failure. Never make it a correctness dependency. Pagination is keyset
  (HMAC-signed cursor), never `skip`.
- **`Server.site_id` is a plain `str`**, not an enum, so a document outlives
  a site being renamed away. **`Server.unread_fields` is recomputed from
  scratch every ingest and never merged.**
- **`?stale=` is the one filter whose clause is an `$expr`, and it must
  stay `$$NOW`-based** (`search.stale_cutoff_expr`, ADR-0029 update): the
  keyset cursor is HMAC-bound to the filter document, so a cutoff rendered
  on the API side would differ per request and fail page two with
  `CURSOR_FILTER_MISMATCH`. Mongo evaluates "now" itself; the `stale`
  *flag* on responses is the API's clock (`schemas.is_stale`). Verified
  live: `find`, `count_documents` and `$group` all take the expression.
- **`HealthSeverity.INFO` is retired (2026-09-13)** and `Health` decodes a
  stored `INFO` as `HEALTHY` through a `mode="before"` validator — the same
  narrowed-enum mechanism as `OpenShiftLifecycle`. Keep it while any
  pre-2026-09-13 document can exist.
- **The fleet gauges are computed from MongoDB on scrape, throttled**
  (ADR-0029, `FleetGaugeRefresher`): the staleness cutoff is rendered as a
  string (above); a never-seen server (`last_seen_at` absent) counts as
  **stale** because BSON sorts missing below any string; collector
  liveness is `max(last_seen_at)`, never `min`; a retired collector's
  label set is cleared each refresh; `servers_partial` ignores a field
  unread on *every* one of a collector's servers. **`Manager.last_run`
  survives `upsert`** because that is a `$set`, not a replace — keep it so.
- **`Manager` carries only what is read**: five never-written fields and
  an index on one were removed 2026-09-13; the index is in
  `RETIRED_INDEXES`.
