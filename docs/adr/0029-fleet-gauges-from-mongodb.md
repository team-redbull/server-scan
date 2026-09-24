# ADR-0029: staleness is a set of gauges the API derives from MongoDB on scrape

Date: 2026-09-12
Status: Accepted

Closes item 0 of CLAUDE.md's "not done" list. Builds on ADR-0016's
`last_seen_at` semantics and ADR-0024's `openshift.last_reported_at`.

## Context

Nothing in this platform could answer "40 hosts have been failing for
two weeks". Every collector is a CronJob: its pod runs for a minute and
exits, so Prometheus never scrapes it, and a collector-side metric can
report anything except its own absence. A CronJob that is suspended,
failing at startup, or simply never deployed to a new cluster is
indistinguishable from one that is fine — the last successful run's data
sits in MongoDB looking current.

The membership jobs (ADR-0024) make it worse: a cluster that stops running
its job leaves every server it held `INSTALLED` forever, and no vendor
collector will ever change that.

What the data already supports: `IngestService` writes `last_seen_at` on
every ingest, **but only when the server's own endpoint answered**
(`ProviderServer.reachable`), so an unreachable run does not make a dead
server look fresh. Every server carries `source_provider`. The membership
jobs write `openshift.last_reported_at` per server. So "did collector X run
recently" and "how many of its servers has it stopped seeing" are both one
`$group` away, and nothing new has to be stored.

## Decision

The API's `/metrics` endpoint exposes gauges derived from MongoDB:

| Gauge | Labels | Answers |
|---|---|---|
| `server_scan_collector_last_seen_timestamp_seconds` | `source_provider` | Is the collector alive? `max(last_seen_at)` — max, not min, so one dead BMC cannot drag it |
| `server_scan_servers_stale` | `source_provider` | How many servers has it stopped seeing? `last_seen_at` older than `INVENTORY_STALE_AFTER_SECONDS`, or absent — a never-seen server is stale, not exempt |
| `server_scan_servers_unreachable` | `source_provider` | Of those, how many are a BMC that did not answer (`reachable=False`) |
| `server_scan_servers` | `source_provider` | Fleet size per collector — the denominator |
| `server_scan_cluster_last_reported_timestamp_seconds` | `cluster` | Is the membership job alive? `max(openshift.last_reported_at)` |
| `server_scan_cluster_servers_held` | `cluster` | How many servers it holds |
| `server_scan_servers_by_health` | `severity` | The fleet's health mix over time |
| `server_scan_servers_in_maintenance` | — | |
| `server_scan_fleet_snapshot_failures_total` | — | The query itself failing |

Added the same day, after the first set was live:

| Gauge | Labels | Answers |
|---|---|---|
| `server_scan_policy_active` | `policy_key` | *What* is wrong — how many servers each health policy fires on. Needs `Health.active_policy_keys`, written at ingest since this ADR; documents from before carry none and simply do not count |
| `server_scan_servers_partial` | `source_provider` | Servers the collector reached but could not fully read — a collector silently returning less |
| `server_scan_collector_last_run_{timestamp_seconds,duration_seconds,servers_fetched,ingest_errors,collection_errors,partial}` | `source_provider` | The most recent run's own outcome, from `Manager.last_run`, which `tools.run_collector` (and the seeder) write at the end of every run. Catches a degraded run in minutes, where `servers_stale` needs the whole window |

**`servers_partial` counts read failures, not structural gaps.** A first
cut counted any server with a non-empty `unread_fields` and read 100% on
every collector, because `hardware.memory.modules` is unread everywhere
(DIMM detail is a platform-wide gap) and a bare BMC has no profile
template. So a field is ignored when it is unread on *every* server of
that collector — the collector never reports it — and counted only when
the same collector reads it elsewhere. Data-driven, so it self-corrects
when a collector starts reporting something, at the cost of one more
aggregation per refresh. Two known limits: a mixed UCS fleet's blades
(no PSUs of their own, beside racks that have them) count as partial on
`hardware.power.psus`; and `unread_fields` itself conflates "could not
read" with "genuinely absent" (a profile with no template), which this
gauge inherits.

`Manager.last_run` is the one stored-shape change with a wrinkle:
`MongoManagerRepository.upsert` used to `replace_one` the whole document
on every ingest, which would have wiped the record between runs. It is
now a `$set` of the configuration fields, and `record_run` writes
`last_run` separately, so a crashed run leaves the previous record in
place — aging, which is the honest signal — rather than blanking it.

Timestamps are Unix seconds with the `_timestamp_seconds` suffix — the
Prometheus convention for "when" — so `time() - metric` is age and stays
correct however late the scrape is.

**Computed on scrape, throttled to once per
`INVENTORY_METRICS_FLEET_REFRESH_SECONDS` (30s).** One `$facet`
aggregation — four groupings in one collection pass — keyed off
`(source_provider, last_seen_at)`, an index that already existed. Scrapes
inside the interval serve the last values. If the query fails the gauges
keep their previous values, the failure counter increments and a warning
is logged; `mongo_ping_failures_total` already says whether MongoDB is
down, so a second signal for the same thing would be noise.

The staleness cutoff is rendered through Pydantic's JSON serializer so it
compares byte-for-byte against the stored ISO strings (ADR-0006 — every
stored datetime is a string, and a `datetime` in the query matches
nothing).

Every API replica exports the same fleet-wide gauges, so the raw series
arrive once per pod. The `PrometheusRule` therefore records a
de-duplicated `server_scan:<gauge>:max` per gauge — the series to query
and to alert on; alerting on the raw series would fire once per replica.
This is deliberate over the alternatives: computing the gauges on one
elected replica adds leader election for a cosmetic gain, and dropping the
`pod`/`instance` labels at scrape time makes two targets write one series,
which Prometheus treats as conflicting samples.

The Helm chart ships a `ServiceMonitor` and a `PrometheusRule` with four
alerts (collector silent, servers stale, cluster silent, snapshot
failing), both off by default because they need the `monitoring.coreos.com`
CRDs vanilla Kubernetes does not have. `deploy/README.md` says what
OpenShift needs on top — user-workload monitoring is off by default there,
and a ServiceMonitor nobody scrapes is the same as none.

## Alternatives rejected

**A background refresh task.** Adds start/stop/crash lifecycle for no
gain: a scrape-time compute means the values are never older than the
scrape interval anyway, and a scrape with MongoDB down serves the last
good values either way.

**Per-server gauges.** 10,000–50,000 time series per scrape. The count
per collector is the right grain for Prometheus; the per-server answer —
*which* servers — belongs in the API and UI, and is the natural follow-up
(a `stale` filter on the inventory).

**A collector-side push (Pushgateway, or the CronJob writing a heartbeat
document).** Both can report a run that happened; neither can report one
that did not. The absence is the whole point.

**`min(last_seen_at)` for collector liveness.** One BMC that has been dead
for a month would make a perfectly healthy collector read as silent
forever. The max says whether the *collector* ran; `servers_stale` says
what it could not read.

## Consequences

- "Is anything not being collected" is a Grafana panel and an alert, not
  a manual Mongo query. `docs/test-redfish-standalone-collector.md` §6's
  manual query is superseded.
- One collection scan every 30s while Prometheus is scraping. Measured
  at 300 seeded servers: sub-millisecond. At 50k it is the same scan
  `GET /api/v1/sites` already does per cache miss.
- The seeded fleet exercises every label: the fake provider already
  seeds a minority of `OPENMANAGE` servers with `reachable=False` and no
  `last_seen_at`, which land in both `servers_stale` and
  `servers_unreachable` — convention 10 satisfied with no generator
  change.
- ~~Not yet: a `stale` filter in the inventory UI, and per-collector run
  duration/ingest counts~~ — both done, see the update below.

## Update (2026-09-13): the UI half, and a correction

**The per-run counters were never missing.** The bullet above claimed run
duration/ingest counts existed only in CronJob logs and needed a push
mechanism. They do not: `tools.run_collector._record_run` writes
`Manager.last_run` on every run, and `FleetGaugeRefresher` already exports
it as `collector_last_run_{timestamp,duration,fetched,ingest_errors,
collection_errors,partial}` gauges. The "push" reasoning applies only to a
*histogram over runs*, which nothing has asked for. Struck rather than
deleted so the correction is visible.

**The stale filter shipped.** Three pieces, all deriving from the same
`last_seen_at` and `INVENTORY_STALE_AFTER_SECONDS` the gauges use:

- **`stale: bool` on `ServerSummary` and `ServerDetail`**, computed at
  response time on the API's clock (`schemas.is_stale`; a never-seen server
  is stale, as in the gauge). The frontend renders a flag and never needs
  the threshold.
- **`?stale=true|false` on `GET /servers` and `/servers/facets`**, with a
  `stale` facet dimension. The clause is
  `{"$expr": {"$lt": ["$last_seen_at", {"$dateToString": {"date":
  {"$subtract": ["$$NOW", window_ms]}}}]}}` — **Mongo evaluates "now" on
  its own clock**, so the filter document is byte-identical from one
  request to the next. That matters because the keyset cursor is
  HMAC-bound to the filter document (`app.domain.services.cursor`); a
  cutoff rendered on the API side would change every request and fail
  page two with `CURSOR_FILTER_MISMATCH`. Verified live against the dev
  Mongo before building on it: `find`, `count_documents` and `$group` all
  accept the expression, and a missing `last_seen_at` compares below any
  string exactly as the gauge assumes (ADR-0006's string rule). The two
  clocks can disagree by skew and nothing more.
- **In the UI**: a `Stale only` checkbox (with its facet count) beside
  `Maintenance only` (both renamed to drop "only", 2026-09-16 update
  below); a `Stale 20h` chip in the State column next to the
  severity badge — outside the severity palette, no glyph, wrapping under
  the badge rather than widening the column (measured: the stale chip adds
  zero width at 1440px) — so a stale row stands out unfiltered, which was
  the actual gap; and the detail page's `Last seen` reads relatively
  ("20 hours ago", exact instant on hover) with a `Stale` badge.

Deliberately not added: a `Seen` column (built, measured, removed — it
pushed the Maintenance column off a 1440px viewport and duplicated the
chip's age on every fresh row), and a per-site stale count on the sites
overview (needs `site_breakdown` extended; nothing asked for it yet).

## Update (2026-09-16): duplicate-name gauges and the UI toggle for them

From the same duplicate-server investigation as ADR-0016's 2026-09-16
serial-fallback update — a real bug (one machine, two documents, empty
serial) surfaced alongside six confirmed-not-bugs (distinct machines
sharing a name across vendors/domains). The serial fix closes the first
class going forward; both classes still deserve visibility, since a
name collision — bug or not — is worth an operator's attention.

| Gauge | Labels | Answers |
|---|---|---|
| `server_scan_duplicate_name_groups` | — | Distinct server names more than one document currently shares |
| `server_scan_duplicate_name_servers` | — | Servers caught in one of those groups (always ≥ 2x the group count) |

Computed in the same `fleet_snapshot` `$facet` as everything else above:
`$group` by `name`, `$match` on `count > 1`. **In the UI**: a `Duplicate`
checkbox beside `Maintenance` and `Stale` (both renamed to drop "only"
the same day — no product meaning change, just three consistent labels)
— entirely client-side, matching `ServerRow.name` collisions over the
whole polled fleet (`rows.ts`'s `nameCounts`, computed before other
filters narrow the set, or a collision split by an unrelated filter
would look unique). No new endpoint, per ADR-0033.

## Update (2026-09-24): a membership job that only matches unmatched hosts had no gauge at all

**The gap.** `server_scan_cluster_last_reported_timestamp_seconds` is
only ever set on a *matched* observation
(`OpenShiftMembershipService._apply` writes `openshift.last_reported_at`
only for a server it found). A `nodes`/`agents` job that runs
successfully on schedule but matches zero hostnames — a broken naming
convention, an inventory the vendor collectors have not caught up to yet
— never sets that gauge, from its very first run. `ServerScanClusterSilent`
alerts on `time() - max(...)`, and a PromQL range query over a series
that has never once existed returns no data, so the alert never fires.
The job can be dead for months and look identical to one that never ran.

Separately, nothing exported *how many* hostnames a healthy run left
unmatched at all — only the CronJob's own stdout and an ERROR log line
per host (`openshift.host_not_in_inventory`), neither of which Prometheus
ever sees, because the job's pod exits before anything could scrape it.

**Decision: the same `Manager.last_run` pattern this ADR already uses for
the vendor collectors, applied to the membership jobs.** `tools.
collect_openshift._record_run` writes a `MembershipRun` (`kind`,
`reported_by`, `observed`, `matched`, `unmatched`, `duration_seconds`,
`partial`) into a new `membership_runs` collection at the end of every
real (non-`--dry-run`) run, exactly where `tools.run_collector._record_run`
already writes `Manager.last_run`. `FleetGaugeRefresher` reads it
alongside `Manager.list_all()` and exports
`server_scan_membership_last_run_{timestamp_seconds,duration_seconds,
observed,matched,unmatched,partial}`, labelled `kind`/`reported_by` so a
`nodes` cluster and an `agents` MCE sharing a name never collide.

This is not the "collector-side push" this ADR's own "Alternatives
rejected" section dismisses — that section is about using a heartbeat as
the *only* signal for total collector silence, which still cannot report
a run that never happened at all (a job that never once deploys
successfully writes nothing, exactly as before; that residual blind spot
is what `kube_job_status_failed` from kube-state-metrics is for, not this
gauge). `unmatched`/`observed`/`matched` are counts no independently-derived
query can produce — an unmatched hostname has no `Server` document to
`$group` by — so a run record is the only way to export them, the same
reasoning that already justifies `Manager.last_run`.

Two new alerts, both off by default with the rest of `prometheusRule`:

| Alert | Fires on | Reads |
|---|---|---|
| `ServerScanMembershipRunSilent` | `server_scan:membership_run_silent_seconds > membershipSilentForSeconds` | The job itself has not completed a real run recently — independent of match rate |
| `ServerScanMembershipUnmatched` | `server_scan:membership_last_run_unmatched:max > membershipUnmatchedThreshold` (default 0) | The job is running fine but is reporting hosts no vendor collector has ingested |

`ServerScanClusterSilent`'s alert and semantics are unchanged — it is
still the per-*server* view (a cluster that stops reporting some, not
all, of what it held), where `ServerScanMembershipRunSilent` is the
per-*job* view — but **its threshold moved off `silentForSeconds` onto
the new `membershipSilentForSeconds` in the same pass**, for the reason
below.

**A shared threshold does not fit both cadences.** `silentForSeconds`
already existed, sized against the vendor collectors'
`collectors.*.schedule` (default every 6h; 43200s = 2 cycles). Reusing it
for `ServerScanMembershipRunSilent` (and, latently, for the pre-existing
`ServerScanClusterSilent`) tolerated 43200s of silence on a `nodes-status`
job that runs every **15 minutes** by default
(`nodes.schedule`/`agents.schedule`) — 48 missed runs before either
alert fired. Caught before it shipped quietly wrong, on the operator's
own estate where both jobs really do run every 15 minutes. Split into a
second required value, `metrics.prometheusRule.membershipSilentForSeconds`
(default 1800s = 2 of the 15-minute cycles), used by both
`ServerScanClusterSilent` and `ServerScanMembershipRunSilent`;
`silentForSeconds` now backs `ServerScanCollectorSilent` alone.

## Alternatives rejected (2026-09-24 update)

**Prometheus Pushgateway.** A new service to deploy and operate in an
air-gapped cluster for one job type, when the existing "write to Mongo,
read on the API's own scrape" mechanism already does the job with zero
new infrastructure.

**Relying on `kube_job_status_failed` alone (kube-state-metrics).** Free
where already deployed, but it only sees a hard crash (exit 1 or 2). This
job's unmatched-hostname case is a legitimate exit 3 — the reconcile
completed correctly — so a generic Job-failure metric would never fire on
it at all.
