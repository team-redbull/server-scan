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
- Not yet: a `stale` filter in the inventory UI, and per-collector run
  duration/ingest counts (those still only exist in CronJob logs, and a
  push mechanism would be the way to get them — see the rejected
  alternative for why that is a different problem from this one).
