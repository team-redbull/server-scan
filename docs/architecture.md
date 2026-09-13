# Architecture

## Purpose

A vendor-neutral inventory of record for a heterogeneous, air-gapped
bare-metal fleet: what servers exist, where, managed by what, classified
how, and how healthy — with every seam a real hardware-manager collector
(Dell OpenManage Enterprise, Cisco UCS Manager, Cisco Intersight, HPE
OneView) and a future OpenShift/MCE agent will need already in place, so
none of those integrations require touching the core.

## Layering

```
backend/app/
  domain/            pure business logic — no I/O, no framework imports
  application/       use-case orchestration over domain + ports
  infrastructure/    MongoDB, Redis, logging — implements domain ports
  api/                FastAPI routers — thin, no business logic
  middleware/         request-id + timing
  observability/      Prometheus metrics
```

Dependency direction is strictly inward: `domain` imports nothing from the
other layers; `infrastructure` implements `Protocol`s declared in `domain`.
This is what lets a real vendor collector, when it lands, become a new
`infrastructure/providers/<vendor>` module plus a `domain` port
implementation — not a change to the classification engine, the health
engine, or any route handler.

```
raw provider facts -> normalized server -> classification -> health policy
                                                    -> persisted state -> UI
```

The fake data generator (slice 1) feeds this same pipeline through a
`ServerInventoryProvider` implementation rather than writing MongoDB
documents directly, so the ingestion path is exercised end-to-end before
any real collector exists.

## Request lifecycle

1. `RequestContextMiddleware` assigns/reuses a request id, binds it into
   `structlog`'s contextvars, and logs one structured `request.completed`
   line per request (replacing uvicorn's plain-text access log).
2. Route handlers depend on services, never touch MongoDB/Redis clients
   directly.
3. Any error — expected (`AppError` subclasses) or not — is rendered by
   `app.exception_handlers` as an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
   Problem Details body (`application/problem+json`), extended with a
   stable `code`, the `request_id`, and structured `details`. RFC 9457 was
   chosen over a bespoke envelope because it is the current IETF standard
   for HTTP API errors, not because any prior project used it.

## Persistence

- **MongoDB is the source of truth.** One `AsyncMongoClient` (PyMongo's
  native async driver — not Motor, which entered its deprecation window in
  May 2026) per process, created during FastAPI's `lifespan` with explicit
  pool and timeout settings, never per-request.
- **Redis is an ephemeral cache only**, cache-aside, with every read path
  falling back to MongoDB on any Redis error or timeout — a Redis outage
  degrades latency, never correctness or availability. `/health/ready`
  reflects this: MongoDB unreachable fails readiness (503); Redis
  unreachable is reported as `"degraded"` without failing it.

## Observability

- **Logging**: `structlog`, with stdlib logging (FastAPI/Uvicorn/PyMongo)
  routed through the same `ProcessorFormatter` pipeline so every log line —
  ours and the framework's — shares one JSON schema in production and one
  readable console format in development.
- **Metrics**: `prometheus-client`'s default registry, scraped at
  `/metrics`. `http_requests_total` and `http_request_duration_seconds`
  are recorded for every request; domain-specific counters (cache hit/miss,
  classification timeouts, etc.) are added alongside the engines that need
  them.
- **Health**: `/health/live` (process liveness, no dependency checks) and
  `/health/ready` (MongoDB required, Redis reported but non-blocking) are
  deliberately unversioned — they're consumed by the container
  orchestrator, not API clients, and must not move if `/api/v1` ever
  becomes `/api/v2`.

## Search, pagination, and caching (slice 1)

- **Search** never sends user input to MongoDB as raw regex. A query is
  lowercased, escaped, and matched as an anchored prefix against
  `search_tokens` (`{"$regex": "^" + re.escape(q)}`) — index-assisted via a
  multikey index, and structurally incapable of ReDoS or an unanchored
  collection scan, unlike unescaped or unanchored regex.
  `app.domain.services.search_tokens.build_search_tokens` builds that field
  at ingest time from name/serial/model/vendor/tags/MACs (both colon and
  bare-hex forms, so a PXE-script-style bare MAC and a colon-form MAC find
  the same server).

  Each value contributes **every suffix of itself that starts at a word
  boundary**, not its bare parts, so an anchored query still finds a
  fragment from the middle of a structured name: `cisco-m6` finds
  `ocp-cisco-m6-bat-yam-128c-1024gb-CIS0000010`. Matching from inside a
  word (`isco`) deliberately does not work — it would need an unanchored
  regex, measured at ~650ms per facet query on 52k servers against ~1-26ms
  for the anchored one. See
  `docs/adr/0025-search-tokens-are-word-boundary-suffixes.md`.

  `search_tokens` is written by `IngestService`, so a token-shape change
  reaches existing documents only on their next collection.
- **Pagination is keyset (cursor-based), never `skip`/`offset`.** The
  cursor is an opaque, HMAC-signed token
  (`app.domain.services.cursor`) binding the current filter/sort
  combination — changing a filter mid-pagination invalidates the old
  cursor with a clear error instead of silently returning wrong results.
  Verified end-to-end against 1,000 seeded servers: every server is
  returned exactly once across a full paginated walk, in any page size.

  **A nullable sort field needs more than `$gt`/`$lt`.** Mongo's range
  operators are type-bracketed — `{$gt: null}` matches nothing and
  `{$lt: "abc"}` skips every null — while the sort itself places nulls
  before every string ascending and after them descending. The two
  disagree, so a naive cursor strands rows silently: no error, just a
  short page. `_cursor_position_clause` carries the null-aware branches
  that make `cluster_name` and `mce_name` sortable
  (`docs/adr/0026-nullable-sort-fields.md`). Sortable fields today are
  `name`, `model`, `updated_at`, `last_seen_at`, `serial`,
  `openshift_state`, `cluster_name` and `mce_name`; each needs an entry in
  `SORT_FIELDS`, a matching `SORT_ACCESSORS` reader, and a compound index
  ending in `_id`.
- **List responses are a lean projection** (`ServerSummary`), not the
  persistence model: no `hardware` subdocument, since at the platform's
  5k-today, 10k-ceiling estate (verified at 50k), shipping full hardware detail on
  every row of a list response is pure waste. Full detail
  (`ServerDetail`) is fetched per-server on demand. Both are dedicated API
  schemas (`app/api/v1/schemas.py`), not `Server` returned as-is — this
  also keeps the MongoDB `_id` alias from ever leaking into a public
  response.
- **A retired index is dropped, not left to an operator.**
  `_create_indexes` reconciles on index *name*, so renaming one creates
  the new index beside the old rather than replacing it — which is how a
  `uniq_system_uuid` that had stopped being declared unique went on
  rejecting real servers. `indexes.RETIRED_INDEXES` names what this file
  no longer declares and `ensure_indexes` drops it on startup, logging
  `mongo.index_retired`. Renaming or removing an index means adding its
  old name there (ADR-0026).
- **Redis caching is cache-aside** with revision-keyed detail cache
  entries (`si:1:srv:{id}:r{revision}` — a write makes the old key
  unreachable without an explicit delete) and short-TTL list-page entries.
  Every `CacheClient` method catches Redis errors and returns a miss rather
  than raising, so a Redis outage degrades request latency, never
  correctness — verified with an integration test that points at an
  unreachable Redis and confirms `GET /servers` still serves correctly
  from MongoDB.

## Classification engine (slice 2)

- **Resolution never relies on MongoDB's natural document order.**
  `app.domain.services.classification.classify` filters the ruleset to
  scope-matching, enabled rules, then sorts by
  `(priority DESC, specificity DESC, order ASC, id ASC)` — a total order
  computed in Python from data already fetched, so the result is
  byte-identical across processes, restarts, and however Mongo happens to
  have stored the documents. Verified by shuffling the same rule set 50
  times and asserting an identical winner every time.
- **Scope specificity is powers of two** (`site:4 + manager:2 + vendor:1`)
  so a more-specific scope strictly outranks a less-specific one
  regardless of how many dimensions are set — a site-scoped rule always
  beats a global rule at equal priority, never a coincidence of how many
  scope fields happen to be filled in.
- **Conflicts are recorded, never silently resolved by luck.** If two
  rules tied on precedence disagree about the outcome, the winner is still
  deterministic (lowest `id`), but the disagreement is persisted on the
  result (`conflicts[]`) rather than hidden — surfacing a rule authoring
  mistake instead of quietly picking one arbitrarily.
- **Regex only ever runs in Python**, never as a MongoDB `$regex`, via a
  `RegexEngine` port (`app.domain.services.regex_engine.RegexModuleEngine`)
  built on the third-party `regex` module's `timeout=` parameter — stdlib
  `re` has no way to bound match time at all. A pattern is rejected at
  *write* time if it can't clear a canary suite of pathological inputs
  within the timeout budget; a pattern that still times out during
  evaluation is skipped and counted, never allowed to stall an entire
  classification run. Empirically, `regex`'s backtracking resists classic
  ReDoS shapes far longer than stdlib `re` does (a `(a+)+$`-style pattern
  needs a ~1000+ character subject to blow up here, not ~40) — the canary
  inputs are sized accordingly; see `regex_engine.py`'s docstring.
- **Preview never sends the draft pattern to Mongo either.** It narrows
  candidates by the parts of `scope` Mongo *can* index (vendor, site_id),
  then evaluates the pattern in Python against the capped candidate set —
  same engine, same safety guarantee as the real classifier.

## Health policy engine (slice 3)

- **A category exists only if `evaluate.CATEGORIES` lists it.** The rollup
  iterates that tuple and nothing else; a policy whose `category` is not
  in it fires, records evidence, and is then dropped before the
  per-category and overall severities are computed. `gpu` was missing
  until 2026-09-13 — "GPU failed" (CRITICAL) fired and the server still
  read HEALTHY overall. Adding a category means `CATEGORIES`, a field on
  `domain.models.health.Health`, `pipeline.health_from_state`, and the
  frontend's `HealthSummary` + overview badges, together.
- **A policy's scope names a set of collectors, not one** (ADR-0030).
  `PolicyScope.manager_types` lists the `ManagerType` values a policy
  applies to; empty means every server. The two fabric-path defaults are
  scoped to `[UCS_CENTRAL, INTERSIGHT]` — servers a fabric interconnect
  owns — and every other default is general. `Server.source_provider` is
  what the engine compares against, so a manager-scoped policy really
  does match now; before ADR-0030 every evaluation passed `None` and such
  a policy matched nothing. The read-only page groups policies by scope
  ("General" first) and sorts each group CRITICAL → MAJOR → WARNING.
- **The override problem** — a site policy must be able to *replace* a
  global default, not just add another alert beside it, while unrelated
  policies keep firing independently — is solved by `policy_key` families
  (`app.domain.models.health_policy`, `app.domain.services.health.
  evaluate.resolve_families`). Policies sharing a `policy_key` compete for
  exactly one winner (highest specificity, then priority); different keys
  are fully independent and all evaluate. A disabled, high-precedence
  family member is how a scope switches a default off entirely — the
  family contributes nothing rather than falling through to the next
  member. Verified live: creating a `GLOBAL_CUSTOM` policy with the same
  `policy_key` as a `SYSTEM_DEFAULT` WARNING policy but `severity: INFO`
  and a higher priority flipped a real seeded server's health from
  WARNING to INFO with zero code change — the platform spec's own
  "changing a threshold must flip the evaluation" requirement, proven
  against live data, not just a unit test.
- **The metric registry is code, not data**
  (`app.domain.services.health.metrics.MetricRegistry`) specifically so an
  operator/metric-type mismatch (`GT` against a list, `IN` with a
  non-list) is rejected when a policy is *saved*, never discovered
  mid-evaluation across the fleet. Extensible per module — a future vendor
  package registers its own metrics at import time; `register()` raises
  loudly on a name collision rather than silently shadowing.
- **Conditions are a closed declarative grammar**
  (`app.domain.services.health.conditions.Condition`) — `all_of`/`any_of`/
  `not`/leaf nodes only, depth- and size-capped, validated against the
  registry before evaluation. No `eval`, no `exec`, no stored code of any
  kind.
- **A value the collector could not read is never a verdict**
  (`docs/adr/0027-unknown-is-not-a-reading.md`). Every fact counts only
  definite readings: `power.failed_psu_count` counts `DOWN` and not
  `UNKNOWN`, `storage.failed_drive_count` counts `CRITICAL`, and so on.
  Network was the one place the comparison ran the other way — good
  readings against a denominator of *every* interface — which marked
  nearly every Cisco server CRITICAL for "no link up" once Cisco
  collectors started reporting vNICs, because UCS reports link state
  `UNKNOWN` on ~99.75% of them. Both link policies now use
  `network.links_known_count` as the denominator;
  `network.interface_count` keeps its literal meaning and rides along as
  evidence. The engine itself stays two-valued — the ADR says what would
  make three-valued evaluation worth it.
- **Message templates are rendered by explicit substitution**, never
  `str.format(**evidence)` — `str.format`'s field syntax reaches attribute
  and index access (`{obj.__class__}`, `{obj[0]}`) even on a template that
  was already validated to reject them, so *validating* the template isn't
  the enforcement, *never calling `.format()` on it at all* is
  (`app.domain.services.health.template`).
- **Preview answers "would this actually fire", not "does the condition
  match in isolation"** — a low-precedence draft can still be shadowed by
  an existing family member for a given server, so `HealthPolicyService.
  preview` splices the draft into the full active policy set and re-runs
  real resolution per candidate. Documented as materially more expensive
  than the classification preview (no Mongo-native condition compilation
  exists yet), bounded by the same scope-filtered, capped candidate scan.

## Fleet gauges: the answer to "did the collector stop"

`app.observability.fleet_gauges` (ADR-0029). A CronJob pod is never
scraped, so no collector can report its own absence; the API does it
instead, from MongoDB, on each `/metrics` scrape throttled to once per
30s. One `$facet` aggregation
(`MongoServerRepository.fleet_snapshot`) yields per-collector totals,
stale counts (`last_seen_at` older than `INVENTORY_STALE_AFTER_SECONDS`,
or absent), unreachable counts and the newest `last_seen_at`; per-cluster
held counts and the newest `openshift.last_reported_at`; the health mix;
and the maintenance count. Timestamps are exported as Unix seconds
(`*_timestamp_seconds`), so `time() - metric` is age. A failed query
leaves the gauges at their last values and increments
`server_scan_fleet_snapshot_failures_total`. The Helm chart ships a
`ServiceMonitor` and a `PrometheusRule` for them, both opt-in.

## Ingestion wires both engines together (slice 2 + 3 integration)

`app.application.services.ingest.IngestService` classifies and
health-evaluates a server in the *same* upsert that ingests it — not a
second write — when `classification_service`/`health_service` are
supplied (both optional, defaulting to `None`, so ingestion still works
before either engine exists and tests of the pipeline in isolation don't
need to construct them). `POST /servers/{id}/reclassify` and
`POST /servers/{id}/health/recalculate` re-run the same two engines
on demand, for "I edited a rule/policy, show me the effect on this server
now" without waiting for the next ingest cycle. `app.application.services.
bootstrap` seeds the platform spec's own acceptance-scenario rules and
policies (four `InstallationType` system defaults — `^ocp4-hypershift`
and `^ocp-` prefixes for `HOSTED_CLUSTER`, an `mce` substring for `MCE`,
and an unconditional `.*` catch-all for `UPI` (2026-09-10 — used to be
`^ocp4`), checked in that order — Dell vendor overrides, the Cisco
one-path-down/two-paths-down fabric policies)
idempotently at startup — "seed only if missing, by name" specifically so
an admin's edit to a system default's `enabled` flag survives every
restart rather than being silently re-armed. See
`app.infrastructure.mongodb.classification_rule_repository.
default_system_rules` for why the patterns are broad, overlapping
catch-alls rather than mutually exclusive by construction, and why that
makes `order` load-bearing.

Verified against a live 1,000-server seeded dataset (regenerated
2026-09-08 against the current four-rule set, superseding an earlier
three-rule count from the same fixture): 349 classified `HOSTED_CLUSTER`,
101 `MCE`, 435 `UPI`, 115 `UNCLASSIFIED` (all via the correct rule); the
exact Cisco fabric acceptance scenario (one path down → WARNING, two
paths down → CRITICAL) reproduced on real seeded servers, not just
fixtures.

Ingest also does two things on every server that are easy to miss because
neither belongs to a collector:

- **It records what could not be read.** For each field a provider
  reported `None` for, `_carry_forward` keeps the stored value *and*
  appends that field's dotted API path to `Server.unread_fields`
  (`hardware.gpus`, `hardware.storage.drives`, `hardware.power.psus`,
  `identity.nic_macs`, …), returned by `GET /api/v1/servers/{id}`. The
  list is recomputed from scratch on every ingest and never merged — a
  path whose value is no longer `None` would otherwise stay flagged
  forever. It exists because carrying forward is not enough on a *first*
  ingest: `Hardware` has no "unknown" state, so an iLO-4 server that
  reported nothing stored `0` drives and rendered as a real, confident
  zero. "Never successfully read" is deliberately not expressible here.
- **It fills in GPU VRAM from the catalog.** No management plane this
  platform collects from reports a GPU's memory size — confirmed against
  both Cisco SDKs, Cisco's own metrics API, Redfish and OneView — so
  `GpuCatalog.enrich` supplies it from a built-in table of 30 NVIDIA and
  AMD datacenter cards, matched on a Cisco PID *or* a vendor model
  string. A value a provider actually read is never overridden; a card
  the table does not answer for keeps `memory_bytes: None` rather than a
  guess. `INVENTORY_GPU_MODELS` overrides the table per identifier; see
  `docs/adr/0021-built-in-gpu-catalog-with-model-matching.md`.

Separately, four hardware models — `StorageDrive`, `Psu`, `Gpu`,
`MemoryModule` — carry a `health_detail: str | None` alongside `health`,
added 2026-09-07. Every provider reduces its own vendor-specific
health/state vocabulary down to the platform's fixed `HEALTHY`/
`WARNING`/`CRITICAL`/`UNKNOWN` (or `UP`/`DOWN`/`DISABLED`/`UNKNOWN` for a
PSU) — deliberately, since that fixed vocabulary is what lets one health
policy count `CRITICAL` drives identically across five vendors' spellings
of "bad". But once reduced, two drives that are both `CRITICAL` can no
longer be told apart — `self-test-failed` and `unconfigured-bad` looked
identical, the exact ambiguity that came up diagnosing UCS/Intersight's
own vocabulary gaps the same day. `health_detail` carries the raw state
through, verbatim, purely for human diagnosis: `app.domain.services.
health` never reads it, only `health` itself. Wired into every provider
that populates `health` today; `MemoryModule.health_detail` exists for
when a collector eventually reports per-DIMM detail — none does yet,
`Memory.modules` is hardcoded empty in `IngestService`.

## What's implemented vs. planned

**Slice 0**: configuration, error model, logging, request context,
Mongo/Redis lifecycle, health probes, metrics, the container image, and
the local dev stack.

**Slice 1**: the `Server`/`Site`/`Manager` domain model and value objects
(MAC normalization across colon/dash/Cisco-dotted/bare-hex forms; BMC
address parsing for the `idrac-virtualmedia://`/`redfish-virtualmedia://`/
`ipmi://` forms vendors actually report), the provider/repository port
contracts, MongoDB repositories and indexes, Redis caching, a deterministic
seeded fake-data generator feeding a real `IngestService` pipeline (not a
shortcut that writes documents directly), and the `GET /api/v1/servers` +
`GET /api/v1/servers/{id}` API with search/filter/sort/cursor pagination —
plus the matching frontend inventory table and server detail page,
including a Connectivity tab that renders a variable number of fabric
groups rather than assuming exactly two.

**Slices 2 and 3**: the classification engine and rules API, and the
health policy engine and policies API (see above), wired into ingestion
and exposed via reclassify/recalculate endpoints.

**Slice 4**: maintenance and an immutable audit trail.

- `PUT`/`DELETE /api/v1/servers/{id}/maintenance` enable/disable a
  server's maintenance window (`app.application.services.
  maintenance_service.MaintenanceService`) — deliberately touching only
  `Server.maintenance`, never `classification`/`health`, so a server can
  be simultaneously HOSTED_CLUSTER, CRITICAL, and in maintenance without
  the three concepts interfering.
- **A maintenance write clears the cached list pages and facet counts**
  (`_invalidate_list_cache`, ADR-0028) — the one write path that does.
  Ingest keeps paying the TTL. Without this the "Maintenance only" filter
  served a page computed before the write, so a server taken back out of
  maintenance kept appearing in a list whose whole meaning is that it is
  in maintenance.
- **Switched from the inventory list, and only from there**
  (2026-09-12): each row carries a switch
  (`features/inventory/MaintenanceToggle`) backed by a row-agnostic
  mutation — the server id is a mutation *variable*, since a hook cannot
  be called per row. Entering maintenance asks for a reason in a small
  card; leaving is one click. `OverviewTab` shows state and reason
  read-only — its own start/end controls were removed the same day at
  the operator's request, so there is one place this is done from and
  one flow to audit. It is the only cell in the table whose click does
  not open the server, so it stops propagation itself rather than
  relying on the row handler's anchor check.
- `audit_events` is append-only by construction, not by convention:
  `MongoAuditEventRepository` exposes only `record()` — no `update`/
  `delete` method exists on the class at all, so no code path in this
  codebase *can* alter or remove a recorded event. `AuditService.record()`
  is the one place every mutation (maintenance changes, and real
  classification/health transitions from `reclassify`/`recalculate`/
  ingestion) goes through. Classification-rule and health-policy CRUD are
  no longer in that list — both are read-only now, see Slice 5 below.
- Ingestion emits `SERVER_CREATED` for genuinely new servers and
  `CLASSIFICATION_CHANGED`/`HEALTH_STATUS_CHANGED` only on a real
  transition — never a generic "updated" event, since ingestion touches
  `last_seen_at` on every server on every run and a naive audit-on-every-
  write would be pure noise with no signal.
- `GET /api/v1/events` and `GET /api/v1/servers/{id}/events` use a
  simpler, unsigned keyset cursor than `servers`' HMAC-signed one — the
  sort order here never varies (`created_at DESC, _id DESC`), and a
  forged/stale cursor on a read-only log has no consequence worse than
  seeing the wrong page. Finding and fixing the cursor was itself the
  most instructive bug this slice produced: every repository in this
  codebase stores `datetime` fields as ISO 8601 *strings* (`model_dump(...,
  mode="json")`), and the first cursor implementation compared a parsed
  Python `datetime` against that stored string in a MongoDB `$lt` query —
  a cross-BSON-type comparison that silently returns wrong results rather
  than raising. See `docs/adr/0006-audit-event-cursor-string-dates.md`.
- `Server.profile_template` — the reusable deployment/configuration
  template a server's profile was provisioned from: UCS Manager's Service
  Profile Template, Intersight's Server Profile Template, HPE OneView's
  Server Profile Template, or a Dell OME Deployment Template. Vendor-
  neutral (`name` + opaque `external_id`), landed alongside slice 4
  because it touches the same `ProviderServer` → `Server` ingestion path.
  Returned by the API from the start; **shown on the server detail
  Overview tab as of 2026-09-08**, labeled in each collector's own
  vendor terminology (`OverviewTab.tsx`'s `PROFILE_TEMPLATE_LABELS`) and
  omitted entirely for `REDFISH_STANDALONE`, which has no template
  concept to show. The same pass added carry-forward for both fields —
  they used to be the one optional pair in the whole ingest pipeline a
  provider's transient read failure could silently blank.

1084 backend tests (unit/integration/api) and the full frontend
lint/typecheck/test/build pipeline pass.

**Slice 5**, as originally built: the classification-rule and
health-policy admin UIs — a form per rule/policy, a debounced live
preview against the backend's preview endpoints, a `ConditionBuilder`
visual builder over the health-policy condition grammar, and a
`ShadowPanel` surfacing which sibling policies a draft would shadow
(ADR-0005). **That entire subsystem was subsequently removed.** Rules and
policies are seeded, code-defined, and ship with every deployment so that
every installation classifies and scores identically — nothing about them
is meant to be end-user-editable, which the write endpoints and their
editors existed to do. `classification_rules.py`/`health_policies.py`
expose only `GET` routes today; `frontend/src/features/rules/RulesPage.tsx`
is the one page, read-only, replacing both editors and their preview
panels. See CLAUDE.md's "Current status" section for the commits
(`f9ab059`, `27b20a8`) and `docs/notes/2026-09-audit-deploy-ci-docs.md`
§4 for how this section came to describe a subsystem that no longer
exists — the corrections there are the reason this paragraph reads the
way it does now rather than the original ~40-line writeup.

**Slice 6**: the 10k/50k performance pass — verifying, against real-scale
data rather than test fixtures, that the platform's stated ~10k-with-
headroom-to-50k target actually holds. See
`docs/adr/0007-scale-verification-and-request-coalescing.md` for the full
writeup; summary:

- `tools/seed_inventory.py` scales linearly (~5.5ms/server through the
  full classify+health-evaluate ingest pipeline) — seeded 50,000 servers
  in the dev database in under 5 minutes.
- `tools/verify_indexes.py` runs `.explain()` for every query shape
  `GET /api/v1/servers` (plus classification/health resolution and
  audit-event reads) can issue, against however many documents are
  currently seeded, and fails if a shape with a supporting index falls
  back to an unexpected `COLLSCAN`. Run against the 50k dataset, it found
  two real index gaps that small-fixture `.explain()` tests could not
  have caught: `last_seen_at`'s index was missing its `_id` tiebreak
  (broke keyset-sortable unfiltered `sort=last_seen_at`), and
  `maintenance.enabled` — a filter whitelisted in `FILTER_FIELDS` — had no
  compound index at all, contradicting `indexes.py`'s own stated design
  rule. Both fixed by adding the missing compound indexes
  (`last_seen_at_id`, `maintenance_enabled_name_id`).
- `tools/loadtest.py` measures real p50/p95/p99 latency under concurrent
  load. It surfaced a cache-stampede tail-latency problem — many
  concurrent identical `GET /api/v1/servers` requests each independently
  missing the 15-second list-page cache and each re-running the same
  expensive query, p99 up to ~4 seconds for a moderately common search
  term. Fixed with `app.infrastructure.singleflight.coalesce`, an
  in-process request-coalescing primitive: concurrent callers for the
  same cache key share one in-flight computation instead of each issuing
  their own. Re-measured p99 for the same scenario: ~156ms.
- A related but distinct tail-latency case — a search term matching zero
  or very few documents forces a full-collection scan since `list_page`'s
  early-stop `limit` never triggers — was found, quantified (~700-800ms
  p99 at 50k), and deliberately left open rather than risk-fixed under
  this slice's time budget; the real fix trades this problem for a
  different one (a blocking in-memory sort that scales with match count)
  whose net direction needs real search-term distribution data this
  platform doesn't have yet. See the ADR's "related, deliberately
  undecided finding" section.
- All of `GET /api/v1/servers`'s filter/sort/search combinations that do
  have a supporting index confirmed IXSCAN (not COLLSCAN) at true 50k
  scale after the fixes above; the unfiltered/`with_count=true` COLLSCANs
  that remain are expected and bounded (a `limit`-capped preview scan, or
  a count the frontend never actually requests).

**Slice 7**: Playwright E2E coverage of the critical admin flows, plus a
real gap it surfaced. See
`docs/adr/0008-e2e-tests-and-maintenance-ui.md` for the full writeup;
summary:

- Maintenance had a fully-built, audited backend (slice 4) but no
  frontend control — `OverviewTab` only ever displayed it read-only,
  since maintenance fell into the gap between slice 1 (inventory) and
  slice 5 (classification/health editors), neither of which owned it.
  Writing an E2E test for "the maintenance flow" is what surfaced there
  was no flow to test. Fixed: `app/api/servers.ts` gained
  `enableMaintenance`/`disableMaintenance`, `app/features/servers/
  hooks.ts` gained the matching mutations, and `OverviewTab` gained an
  inline start/end-maintenance control. (That control has since moved
  to the inventory list — see slice 4 above; the detail page is
  read-only for maintenance again, by request rather than by omission.)
- `frontend/e2e/` (Playwright) covers inventory search/detail/tabs,
  classification-rule create+preview+disable+delete, health-policy
  create+shadow-panel+delete, and maintenance enable/disable — run three
  times back to back with zero leftover test data (`test.afterEach`
  cleanup via direct API calls, keyed by a per-run unique name).
- A real, confirmed Chromium behavior broke the obvious `getByLabel`
  selector approach: a `<label>Text<select>…option…</select></label>`
  field's computed accessible name (and `textContent`) concatenates the
  label text with every option's text, so `getByLabel("Vendor")`
  intermittently matched the *Source* field instead (its option list
  contains `"VENDOR_CUSTOM"`). Fixed with `labeledField()`, an XPath
  `text()`-axis helper that matches only a label's own direct text node.
  Documented as an ADR, not just a code comment, since it will recur the
  moment a new form field is added and a future test reaches for
  `getByLabel` again.
- New `e2e` CI job: real backend + MongoDB + Redis + a small (300-server)
  seeded dataset + the frontend dev server, running the full suite
  headless with `--with-deps` Chromium.

**First real collector**: Cisco UCS Manager — the first vendor
integration that isn't `FakeProvider`. See
`docs/adr/0009-ucs-manager-collector.md` for the full writeup; summary:

- `app.infrastructure.providers.ucs_manager` implements the same
  `ServerInventoryProvider` seam `FakeProvider` already does, over
  Cisco's official `ucsmsdk` Python SDK (synchronous — wrapped in
  `asyncio.to_thread` throughout, since no async UCS SDK exists).
  Identity, hardware summary, service-profile/template resolution, NIC
  MACs, fabric attachments, and CIMC/BMC address are all wired up. CPU
  model string and per-drive storage detail started as explicit v1 scope
  cuts (see the ADR) but were built and, as of the 2026-09-07 live
  UCS Central run below, confirmed populated on real hardware — no
  longer a gap.
- A connection-resolution seam, `app.domain.ports.credentials.
  CredentialResolver`, and its one implementation,
  `EnvConnectionResolver` — one endpoint plus login per `ManagerType`,
  read from settings (`INVENTORY_UCS_CENTRAL_IP`/`_USERNAME`/`_PASSWORD`,
  and the same shape for OneView, OME and Intersight). That
  is the whole of a collector's connection config: no `Manager` document
  to create first, no credentials volume to mount. Resolution is keyed on
  the manager *type*, not a per-manager reference, because this platform
  runs one endpoint per vendor — UCS Manager's multi-domain story is the
  UCS Central collector enumerating its domains at collection time, which
  is also why `UCS_MANAGER` carries a login but no endpoint
  (`INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD` only; see
  `docs/adr/0014`'s 2026-08-17 update).
  A half-configured vendor raises `ManagerNotConfiguredError` naming the
  missing variables rather than attempting a login that fails as "bad
  credentials", and `ManagerConnection.__repr__` redacts the password so
  it cannot leak through a traceback.
- `tools/run_collector.py --manager-type UCS_CENTRAL` — the CLI a
  Kubernetes `CronJob` invokes: resolves that type's connection, runs it
  through the same `IngestService` pipeline the fake-data seed script
  uses (classify, health-evaluate, audit, upsert — one write per server),
  and writes back a `Manager` document as a projection of the config so
  the API can resolve `Server.manager_id`. `--dry-run` prints what the
  provider reports and writes nothing; `--debug-xml` dumps every XML
  request/response.
- The Helm chart's `collectors.*` values render into a single `Secret`,
  injected with `envFrom` so passwords are not readable in the pod spec;
  `collectors.existingSecret` defers to a Secret owned by Vault or
  External Secrets instead. The collector shares the API's own container
  image (`Containerfile` also copies `tools/`) rather than building a
  second one. The parallel plain-OpenShift manifest set was removed —
  it duplicated the chart with nothing checking the two agreed, and had
  already drifted; `helm template` covers that case on demand.
- **Validated end to end against a live Cisco UCS Platform Emulator**
  (UCSPE 4.2(2aS9)) — see ADR-0009's validation sections for what that
  proved, disproved and could not settle. Several defects were only
  visible against real hardware: a queried MO class that does not exist
  and aborted every run, a BMC filter that matched nothing, a whole
  class of adapter interface never collected (leaving most servers with
  no MACs or fabric attachments), fabric path counts that were always
  zero because UCS state strings were passed through unmapped, and
  servers named after their chassis slot rather than their service
  profile — which silently defeated both site parsing and
  classification.
- **Validated again, 2026-09-07, against the user's own real, air-gapped
  UCS Central domain** (`tools/run_collector.py --manager-type
  UCS_CENTRAL --dry-run`, plus a UI check of one server and an SSH `free`
  on its OpenShift node) — this closed every scope cut and open question
  the UCSPE run above could not settle on its own. `total_memory`'s MB
  assumption is now SETTLED correct: UCS's own UI reported the identical
  raw total the collector used, and the node's lower `free` reading is
  ordinary BIOS/kernel-reserved memory, not a units bug. `cpu_model` and
  per-drive storage detail are confirmed populated on real hardware, not
  just present in the mapping code. Fabric interconnect
  `fabric_model`/`fabric_serial` were already populated (previously
  undocumented), and `fabric_name` — the domain's own `topSystem.name`
  cluster name, since UCS Manager has no per-FI hostname of its own — was
  built and wired into every fabric attachment the same day, confirmed
  live; `fabric_id` still has no source and stays `None`. Sampling 18,117
  disks and 15,459 physical adapter interfaces found and fixed two real
  vocabulary gaps — `_DISK_HEALTH_MAP` was missing `offline` and
  `self-test-failed`, `_OPER_STATE_MAP` was missing five
  `AdaptorExtEthIf` values — while confirming `NA`/`unknown` (disk) and
  `indeterminate` (interface) are deliberately left unmapped: Cisco's own
  terms for "doesn't apply", "no verdict" and "cannot be determined", not
  gaps in the map. See ADR-0009's three 2026-09-07 update sections for
  the full write-up.
- Every `ManagerType` now has a collector except `UCS_MANAGER`, which
  deliberately has no entry point of its own (it is reached through
  `UCS_CENTRAL`); `tools.run_collector` says so in as many words rather
  than claiming a missing feature. Intersight reuses the same three
  settings with different meanings: it signs requests with an API key, so
  `username` is the API Key ID and `password` the secret key.

### Standalone Redfish collector (`REDFISH_STANDALONE`)

The second real collector, and the first that points at no manager at
all. It reaches a BMC directly over DMTF Redfish — any conformant one, so
a Cisco CIMC that Intersight cannot yet manage sits alongside an iDRAC
and a current iLO. HPE iLO 4 is excluded on *conformance* grounds rather
than vendor grounds: it answers `/redfish/v1` with pre-Redfish property
spellings, and the collector rejects it at the service root before
sending a credential.

`app.infrastructure.providers.redfish` mirrors `ucs_manager`'s split —
`client.py` (I/O, session lifecycle, TLS, the retry taxonomy),
`mapping.py` (pure payload -> `ProviderServer`), `provider.py` (fan-out
and budgets) — plus `targets.py`, which has no analogue elsewhere because
no other collector has a fleet list to parse. Nothing is shared with
`ucs_common`: that module is DN structure and Cisco SDK presence
semantics, and Redfish has neither.

**Three properties invert what the other collectors assume.**

*The fleet list is input, not discovery.* Nothing enumerates standalone
machines, so a mounted TOML inventory does. It is normally the only
collection filter — the shared `INVENTORY_COLLECTOR_NAME_PATTERN` is
deliberately not applied, because a BMC does not know the server's
`ocp4-...` name and `^ocp` would discard every listed host. Only this
collector's own `INVENTORY_REDFISH_NAME_PATTERN` overrides that; every
manager type has such an override, reconciled with the shared default in
`tools.run_collector.resolve_name_pattern`. Credentials resolve
host -> host-named -> group -> defaults -> a fleet-wide fallback, and the
whole file is validated before a single connection opens: an unknown
group, an undefined credential, a duplicate host, an address carrying
credentials, or a TLS opt-out with no written reason each fail the run
naming what to fix. Failing closed matters more than it looks — a
typo'd group name that fell through to the default credential would send
a shared service account to a machine it was never meant for.

*Cost is per server.* A UCS Central run costs ~11 round trips for the
whole Cisco fleet; this costs ~25 against each BMC, on embedded hardware
that degrades when polled. Bounded fleet concurrency, a per-host
wall-clock budget and an in-process run budget are therefore correctness
requirements. The run budget must trip before the CronJob's
`activeDeadlineSeconds`, or the pod is killed with no summary at all.
Hosts are shuffled each run so a truncated sweep does not starve the same
slow hosts forever, and servers stream out as each host finishes rather
than being gathered, so a killed run has already persisted what completed.

*Failure is routine.* Some of several hundred independent BMCs are always
down, so per-host failures accumulate in `collection_errors` (exit 3,
PARTIAL) and the run continues. Only a systemic authentication failure
stops it: a rejected login is never retried, a credential is disabled
after enough *distinct* hosts reject it, and a run-wide failure budget
covers the estate where every BMC has its own account — there the
per-credential counter never trips while accounts lock one at a time.
Both bound damage rather than preventing lockout, and ADR-0016 says so
explicitly rather than over-claiming.

Two guards exist because the collector parses JSON from a device it does
not fully trust: an `@odata.id` that is not a relative path under
`/redfish/v1` is refused rather than followed, and redirects are never
followed — both would retarget the next request, which carries the
session token.

Full design, evidence and open questions: `docs/adr/0016`. Runbook:
`docs/test-redfish-standalone-collector.md`.

### Cisco Intersight (`INTERSIGHT`)

The third collector, and the first whose cost does not scale with the
fleet: every child managed object carries an inverse reference to its
owner, so each sub-resource is listed once for the whole estate and
joined in memory — on the order of 120 requests for 10,000 servers,
against the Redfish collector's ~25 *per BMC*. The trade is memory: the
join tables are held for the length of the run and grow with the fleet,
which no other collector's do, and `$select` on every query is what keeps
that affordable.

It is not a login. Intersight has no username/password path for its REST
API at all — every request is HTTP-Signature signed — so its credentials
are `INVENTORY_INTERSIGHT_API_KEY_ID` and `_API_KEY_PEM`, with the PEM
riding in the environment variable rather than a mounted file. Signing is
hand-rolled on `httpx` + `cryptography` rather than pulling the official
SDK, a 57.6 MB wheel of 10,112 generated modules for the eight we would
touch; the RSA construction was verified byte-identical against it.

It deliberately does not collect `ManagementMode == UCSM` servers: those
are exactly the ones `UCS_CENTRAL` owns, and since ingest correlates on
`(vendor, serial_normalized)`, collecting both would make one document's
`source_provider` and every mapped field flip on whichever CronJob ran
last. `docs/adr/0017-intersight-collector.md` has the design.

**Validated against the user's on-prem Private Virtual Appliance, twice**
(2026-09-01, then again 2026-09-07). Auth, name resolution, and the
`TotalMemory`-as-MiB assumption were confirmed on the first run, along
with `cpu_model` and per-drive storage once a `ComputeBoard` join gap
that had zeroed out both was found and fixed. The second run found and
fixed two more real gaps: the GPU catalog never matched Intersight's own
product-name spelling (a form-factor word before the capacity, plus a
trailing wattage figure neither had a rule for), and
`equipment.Psu.OperState`/`storage.PhysicalDisk.Health` both report a
plain `"OK"` — a spelling neither UCS's own vocabulary map had, so PSUs
and drives were silently reading `UNKNOWN` instead of `UP`/`HEALTHY`.
Both are fixed. What remains unconfirmed is narrower than before: the
DOWN/CRITICAL counterpart of that same vocabulary, since no PSU, GPU or
drive on that tenant has ever reported a failure state. See
`docs/adr/0017-intersight-collector.md`'s "A second field pass
(2026-09-07, same tenant)" section.

### Dell (`OPENMANAGE`) — identity from OME, hardware from each iDRAC

The one collector that reads from two places on purpose. Two bulk REST
calls against the single OpenManage Enterprise appliance enumerate every
server profile and managed device, which is where the operator's name,
the deployment template, the service tag and the iDRAC address come from;
the hardware behind those addresses is then read from each server's own
BMC over Redfish, reusing `app.infrastructure.providers.redfish` rather
than a second mapping. The split is on provenance, not convenience: only
OME knows the name that site parsing and classification need, and only
the BMC reports measured values.

It is therefore the one collector that needs **two** logins —
`INVENTORY_OME_USERNAME`/`_PASSWORD` for the appliance and
`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` for a shared read-only iDRAC
account — and the run refuses to start without both, naming the
variables. Full design: `docs/adr/0020-dell-identity-from-ome-hardware-
from-redfish.md`; verified field facts: `docs/dell-collectors.md`.

**Has never been run against a live appliance.** The hardware half
reuses `app.infrastructure.providers.redfish`'s mapping, so it inherits
`REDFISH_STANDALONE`'s own validation for that half only — the OME
identity/name-resolution half has no live-hardware proof of its own. With
UCS Manager/Central, Intersight and OneView all now validated against
real equipment (see above and below), `OPENMANAGE` is the one collector
left with no live-hardware pass at all.

### HPE (`ONEVIEW`) — one source, at every iLO generation

The fourth vendor collector, and the one whose shape is most likely to be
guessed wrong. The estate runs iLO 4, 5 and 6 in the same racks, and iLO
4 predates useful Redfish coverage. Copying the Dell split would put a
per-generation branch in the collection path and give one vendor's
servers two different sets of field provenance, so **the user's explicit
decision was one collection standard for all HP hardware: OneView, for
every server, whatever its iLO generation.** No Redfish pass, no
`RedfishTarget`, no BMC credentials, no branch on `mpModel`. What OneView
cannot report for an older machine is `None` — carried forward by ingest,
and listed in `Server.unread_fields` — never zero.

The cost is three bulk calls per appliance: `GET /rest/server-hardware`
returns the complete object per member rather than a summary, and
`expand=all` folds each server's DIMMs, drives, GPUs and PCI devices into
the same response. The one exception is power supplies, which OneView
will not expand reliably and which therefore cost one request per server
(`INVENTORY_ONEVIEW_COLLECT_PSUS`, on by default).

Four traps are worth knowing before touching the mapping, each with its
HPE source in `docs/hpe-collectors.md`:

- **The name comes from the server profile.** `server-hardware.name` is
  the enclosure-and-bay location, and `serverName` is an OS hostname via
  HPE's Agentless Management Service — both are decoys, the same trap
  ADR-0009 records for UCS blades named after their chassis slot. Hardware
  with no assigned profile is skipped and counted.
- **`processorCoreCount` is per processor**, so `cpu_cores` is
  `processorCount * processorCoreCount`. Passing it through unmultiplied
  halves every two-socket server, silently.
- **`memoryMb` is MiB, and HPE says so inline** — no assumption, unlike
  Intersight's undocumented `TotalMemory`.
- **`count=-1` means 64, not "all"**, on `/rest/server-profiles`, with a
  256 ceiling and truncation HPE documents without saying whether paging
  passes it. An explicit `count` is always sent and a short read is logged
  at ERROR rather than hidden.

**Validated against a live appliance 2026-09-07** (821 servers) — core
count and profile paging both confirmed correct, and a real storage
mapping bug found and fixed the same day (`LocalStorage`'s data is a list
of per-controller objects, not a flat drive list). See
`docs/adr/0022-oneview-only-hpe-collector.md`'s "Results, 2026-09-07".
GPU field mapping remains unverified — that estate has no GPU-bearing
HPE server.

### Every collector now reports power supplies

`ProviderServer.psus` was added on 2026-09-01 and was dead for a while:
the health engine's `power.psu_count` and `power.failed_psu_count`
metrics had nothing to read, so a server with a dead PSU reported HEALTHY
on power exactly like one with two good ones. It is now populated by all
five collectors — Intersight and UCS Manager/Central for rack units (a
blade's supplies belong to its shared chassis, not to the blade), the
shared Redfish mapping's `psus_from_supplies` for Dell and every
standalone BMC, and OneView for HPE.

Two rules are shared across every one of those mappings:

- **An absent supply is dropped, never counted as failed.** A four-bay
  chassis with two supplies fitted is not a server with two failed PSUs,
  and counting it as one would permanently misreport
  `power.failed_psu_count` for every partially populated chassis.
- **A PSU's `health` is `UP`/`DOWN`/`DISABLED`/`UNKNOWN`, not a
  `HealthSeverity`.** A policy written against `power.failed_psu_count`
  is comparing against `"DOWN"`, not `"FAILED"`. Redfish's `Warning`
  deliberately maps to `UNKNOWN` rather than `DOWN`: a degraded supply
  still delivering power has not lost redundancy, and counting it would
  raise CRITICAL on a healthy server.

### Cluster membership: a second kind of job, and a reconcile

Every collector above answers "what hardware exists". None can answer "is
anything using it": a vendor manager sees a blade as `associated` whether
it runs production or nothing at all. Before 2026-09-09 the only cluster
signal was `Classification.installation_type`, a regex verdict on the
hostname — which says what a machine was *named* to be, so a freed server
and a running one were indistinguishable.

`tools/collect_openshift.py` runs *inside* a cluster and writes
`Server.openshift` and nothing else. Two sources: `--source nodes` on
every cluster (its own worker nodes) and `--source agents` on an MCE hub
(its Agents, `INSTALLED` when bound to a cluster,
`INSTALLED_TO_INVENTORY` when bound to nothing). Correlation is by
hostname; `spec.hostname` wins and `status.inventory.hostname` is the
fallback, which is what makes it work across Cisco and Dell alike.

Three things are worth carrying:

- **It reconciles a set, it does not write what it saw.** Nothing in
  Kubernetes reports a removal — a freed server just stops appearing — so
  a job that only wrote observations would leave every server it ever saw
  `INSTALLED` forever. Each run therefore also frees the servers still
  naming its cluster that it did *not* see this time.
- **The scope of that free is the safety property.** A job may only ever
  release servers that already name its own cluster. That is what lets
  one job per cluster run independently: a broken one cannot free
  another cluster's machines.
- **It refuses twice.** A failed read exits non-zero and writes nothing;
  a *successful* read returning nothing also refuses, because an empty
  answer is far more likely a broken selector or an RBAC change than a
  genuinely emptied cluster — and acting on it would free everything.

Deployment is one Helm release per cluster
(`deploy/helm/openshift-membership`), separate from the platform's own
chart because these run somewhere else entirely. Design and the field
trim that took `OpenShiftLifecycle` to five fields:
`docs/adr/0024-openshift-cluster-membership.md`.

### CI supply chain

Every GitHub Action is pinned to a commit SHA rather than a tag, because
a tag can be re-pointed by whoever controls the action's repository and
this repo's `publish` job holds `contents: write` plus a GHCR token. The
release-tagging step is `PaulHatch/semantic-version` (node24) followed by
an explicit `git tag && git push`, with a guard between them that refuses
an empty, duplicate or backwards version — it replaced an action that
declares the now-removed node20 and had no upgrade available.

Nothing updates itself: Dependabot was configured and removed after it
edited `requirements.txt`, a generated air-gap export, as though it were
a source manifest. Keeping the pins current is a documented manual pass
(`CLAUDE.md`'s "Keeping CI current"), and the reasoning behind all of it
is `docs/adr/0013`.

### Staleness detection is the collector's missing half

**Since 2026-09-10 this covers the membership jobs too, where it bites
harder.** A collector that stops running leaves stale hardware facts; a
membership job that stops running leaves every server it holds
`INSTALLED` forever, because `AVAILABLE` is only ever reached by that
job's own reconcile. The inventory then quietly overstates how much of
the fleet is in use — and the sites page's Available card, the one an
operator would act on, is exactly what goes wrong.


A CronJob pod lives minutes and exits, so Prometheus never scrapes it —
no metric the Redfish collector emits could report its own *absence*,
which is exactly the failure that matters when hosts quietly stop
answering. The answer is gauges derived from MongoDB's `last_seen_at`
(written on every ingest, currently read by nothing) exported by the API
process. Until that lands, staleness is a documented manual query, and
`docs/test-redfish-standalone-collector.md` §6 carries it rather than
implying coverage that does not exist.

Real authentication is designed (see the session's approved plan) but
lands in a subsequent slice — this document will gain a section and an
ADR once it's implemented, rather than describing not-yet-existing code
as done.

## Further reading

- `docs/arc42.md` — the structured architecture overview (arc42): goals,
  constraints, context, deployment view, quality scenarios, and the
  risk/technical-debt register. It links *into* this document for the
  subsystem detail rather than restating it, so start there for the shape
  of the system and come back here for how a part works.
- `docs/adr/` — architecture decision records, added as decisions are made
  (not written speculatively ahead of the code).
- `deploy/` — OpenShift and Helm deployment manifests, including
  `deploy/helm/openshift-membership/README.md` for the per-cluster jobs.
- `scripts/check_comment_density.py` — the CI gate behind CLAUDE.md's
  convention 8, with its baseline of pre-existing debt beside it.
