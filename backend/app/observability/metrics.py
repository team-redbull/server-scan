"""Prometheus metrics.

`prometheus-client`'s default registry is used directly (not a custom
registry passed around via DI) — that's the library's own documented
pattern and matches what every Prometheus-scraping tool expects to find
mounted at `/metrics` with no extra wiring.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    labelnames=("method", "path", "status"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=("method", "path"),
)

mongo_ping_failures_total = Counter(
    "mongo_ping_failures_total",
    "Count of failed MongoDB readiness pings",
)

cache_operations_total = Counter(
    "cache_operations_total",
    "Cache operations by outcome",
    labelnames=("operation", "outcome"),
)

# Fleet gauges, refreshed from MongoDB on scrape — see ADR-0029 and
# `app.observability.fleet_gauges`. The `_timestamp_seconds` pair follow
# the Prometheus naming convention for "when", so `time() - metric` is age.
servers_total = Gauge(
    "server_scan_servers",
    "Servers in the inventory, by the collector that owns them",
    labelnames=("source_provider",),
)
servers_stale = Gauge(
    "server_scan_servers_stale",
    "Servers not successfully collected within INVENTORY_STALE_AFTER_SECONDS",
    labelnames=("source_provider",),
)
servers_unreachable = Gauge(
    "server_scan_servers_unreachable",
    "Servers whose BMC could not be reached on the most recent collection",
    labelnames=("source_provider",),
)
servers_partial = Gauge(
    "server_scan_servers_partial",
    "Servers whose most recent collection could not read every field",
    labelnames=("source_provider",),
)
policy_active = Gauge(
    "server_scan_policy_active",
    "Servers each health policy is currently firing on",
    labelnames=("policy_key",),
)
collector_last_run_timestamp = Gauge(
    "server_scan_collector_last_run_timestamp_seconds",
    "Unix time a collector's most recent run finished",
    labelnames=("source_provider",),
)
collector_last_run_duration = Gauge(
    "server_scan_collector_last_run_duration_seconds",
    "Wall-clock seconds the most recent run took",
    labelnames=("source_provider",),
)
collector_last_run_fetched = Gauge(
    "server_scan_collector_last_run_servers_fetched",
    "Servers the most recent run fetched from the vendor",
    labelnames=("source_provider",),
)
collector_last_run_ingest_errors = Gauge(
    "server_scan_collector_last_run_ingest_errors",
    "Servers the most recent run fetched but could not ingest",
    labelnames=("source_provider",),
)
collector_last_run_collection_errors = Gauge(
    "server_scan_collector_last_run_collection_errors",
    "Hosts the most recent run could not collect, benign or not",
    labelnames=("source_provider",),
)
collector_last_run_partial = Gauge(
    "server_scan_collector_last_run_partial",
    "1 if the most recent run exited PARTIAL (did not see the whole fleet)",
    labelnames=("source_provider",),
)
collector_last_seen_timestamp = Gauge(
    "server_scan_collector_last_seen_timestamp_seconds",
    "Unix time a collector last successfully read any server",
    labelnames=("source_provider",),
)
cluster_servers_held = Gauge(
    "server_scan_cluster_servers_held",
    "Servers an OpenShift cluster reports holding",
    labelnames=("cluster",),
)
cluster_last_reported_timestamp = Gauge(
    "server_scan_cluster_last_reported_timestamp_seconds",
    "Unix time an OpenShift cluster's membership job last reported",
    labelnames=("cluster",),
)
servers_by_health = Gauge(
    "server_scan_servers_by_health",
    "Servers by overall health severity",
    labelnames=("severity",),
)
servers_in_maintenance = Gauge(
    "server_scan_servers_in_maintenance",
    "Servers currently in maintenance mode",
)
duplicate_name_groups = Gauge(
    "server_scan_duplicate_name_groups",
    "Distinct server names shared by more than one document",
)
duplicate_name_servers = Gauge(
    "server_scan_duplicate_name_servers",
    "Servers whose name is shared by another server's document",
)
fleet_snapshot_failures_total = Counter(
    "server_scan_fleet_snapshot_failures_total",
    "Fleet gauge refreshes that failed, leaving the previous values in place",
)
