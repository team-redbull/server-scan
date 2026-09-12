"""Refresh the fleet gauges from MongoDB, at most once per interval.

Computed on scrape rather than by a background task — see ADR-0029.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Protocol

import structlog

from app.domain.ports.repository import FleetSnapshot
from app.observability import metrics
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)


class FleetSnapshotSource(Protocol):
    """The one repository method the refresher needs."""

    async def fleet_snapshot(self, *, stale_before: datetime) -> FleetSnapshot:
        """
        Summarise the fleet — see `ServerRepository.fleet_snapshot`.

        Args:
            stale_before (datetime): The staleness cutoff.

        Returns:
            FleetSnapshot: The summary.
        """
        ...


def _epoch(iso: str | None) -> float | None:
    """
    Convert a stored ISO 8601 string to Unix seconds.

    Args:
        iso (str | None): The raw stored timestamp, `Z`-suffixed.

    Returns:
        float | None: Seconds since the epoch, or `None` for no timestamp.
    """
    if iso is None:
        return None
    return datetime.fromisoformat(iso).timestamp()


def apply_snapshot(snapshot: FleetSnapshot) -> None:
    """
    Write one snapshot into the gauges, clearing label sets it no longer names.

    Args:
        snapshot (FleetSnapshot): What the repository reported.
    """
    for gauge in (
        metrics.servers_total,
        metrics.servers_stale,
        metrics.servers_unreachable,
        metrics.collector_last_seen_timestamp,
        metrics.cluster_servers_held,
        metrics.cluster_last_reported_timestamp,
        metrics.servers_by_health,
    ):
        gauge.clear()

    for row in snapshot.by_provider:
        provider = row.source_provider or "unknown"
        metrics.servers_total.labels(source_provider=provider).set(row.total)
        metrics.servers_stale.labels(source_provider=provider).set(row.stale)
        metrics.servers_unreachable.labels(source_provider=provider).set(row.unreachable)
        seen = _epoch(row.last_seen_at)
        if seen is not None:
            metrics.collector_last_seen_timestamp.labels(source_provider=provider).set(seen)

    for cluster in snapshot.by_cluster:
        metrics.cluster_servers_held.labels(cluster=cluster.cluster_name).set(cluster.held)
        reported = _epoch(cluster.last_reported_at)
        if reported is not None:
            metrics.cluster_last_reported_timestamp.labels(cluster=cluster.cluster_name).set(
                reported
            )

    for severity, count in snapshot.by_health.items():
        metrics.servers_by_health.labels(severity=severity).set(count)

    metrics.servers_in_maintenance.set(snapshot.in_maintenance)


class FleetGaugeRefresher:
    """Throttles fleet-gauge refreshes so concurrent scrapes share one query."""

    def __init__(
        self, repo: FleetSnapshotSource, *, stale_after_seconds: int, min_interval_seconds: float
    ) -> None:
        """
        Bind the refresher to a repository and its two knobs.

        Args:
            repo (FleetSnapshotSource): Where the snapshot is read from.
            stale_after_seconds (int): Age past which a server is stale.
            min_interval_seconds (float): Shortest gap between two queries.
        """
        self._repo = repo
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._min_interval = min_interval_seconds
        self._last_refresh: float | None = None
        self._lock = asyncio.Lock()

    async def maybe_refresh(self) -> bool:
        """
        Refresh the gauges unless a refresh ran within the interval.

        Never raises: a failed query is counted and logged, and the gauges
        keep their previous values.

        Returns:
            bool: Whether a query was actually run.
        """
        async with self._lock:
            now = time.monotonic()
            if self._last_refresh is not None and now - self._last_refresh < self._min_interval:
                return False
            try:
                snapshot = await self._repo.fleet_snapshot(
                    stale_before=utcnow() - self._stale_after
                )
            except Exception as exc:
                metrics.fleet_snapshot_failures_total.inc()
                logger.warning("metrics.fleet_snapshot_failed", error=str(exc))
                return False
            apply_snapshot(snapshot)
            self._last_refresh = now
            return True
