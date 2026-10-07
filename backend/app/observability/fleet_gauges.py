"""Refresh the fleet gauges from MongoDB, at most once per interval.

Computed on scrape rather than by a background task — see ADR-0029.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Protocol

import structlog

from app.domain.models.manager import Manager
from app.domain.models.openshift import MembershipRun
from app.domain.ports.repository import AuditStats, FleetSnapshot
from app.observability import metrics
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

PRUNED_EVENT_TYPE = "SERVER_PRUNED"


class ManagerSource(Protocol):
    """The one repository method the run gauges need."""

    async def list_all(self) -> list[Manager]:
        """
        Every manager document, run record included.

        Returns:
            list[Manager]: All managers.
        """
        ...


class MembershipRunSource(Protocol):
    """The one repository method the membership run gauges need."""

    async def list_all(self) -> list[MembershipRun]:
        """
        The most recent run of every membership job.

        Returns:
            list[MembershipRun]: One entry per `kind`/`reported_by` pair.
        """
        ...


class PrunedSource(Protocol):
    """The one audit-repository method the pruned gauge needs."""

    async def count_by_provider_since(self, event_type: str, since: datetime) -> dict[str, int]:
        """
        Count audit events of one type since a cutoff, per provider.

        Args:
            event_type (str): The audit `event_type`.
            since (datetime): Inclusive lower bound on `created_at`.

        Returns:
            dict[str, int]: Count per `source_provider`.
        """
        ...


class AuditStatsSource(Protocol):
    """The one audit-repository method the audit-trail gauges need."""

    async def audit_stats(self) -> AuditStats:
        """
        How many audit events are stored and when the oldest one was created.

        Returns:
            AuditStats: The estimated total and the oldest `created_at`, if any.
        """
        ...


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


def apply_snapshot(
    snapshot: FleetSnapshot,
    managers: list[Manager],
    membership_runs: list[MembershipRun],
    pruned: dict[str, int] | None = None,
    audit: AuditStats | None = None,
) -> None:
    """
    Write one snapshot into the gauges, clearing label sets it no longer names.

    Args:
        snapshot (FleetSnapshot): What the server repository reported.
        managers (list[Manager]): Every manager, for the run gauges.
        membership_runs (list[MembershipRun]): Every membership job's most
            recent run, for the membership run gauges.
        pruned (dict[str, int] | None): Servers pruned in the last 24h per
            provider; `None` leaves the pruned gauge untouched.
        audit (AuditStats | None): The audit trail's size and oldest event;
            `None` leaves the audit gauges untouched.
    """
    for gauge in (
        metrics.servers_total,
        metrics.servers_stale,
        metrics.servers_unreachable,
        metrics.servers_partial,
        metrics.policy_active,
        metrics.collector_last_seen_timestamp,
        metrics.collector_last_run_timestamp,
        metrics.collector_last_run_duration,
        metrics.collector_last_run_fetched,
        metrics.collector_last_run_unchanged,
        metrics.collector_last_run_ingest_errors,
        metrics.collector_last_run_collection_errors,
        metrics.collector_last_run_partial,
        metrics.cluster_servers_held,
        metrics.cluster_last_reported_timestamp,
        metrics.servers_by_health,
        metrics.membership_last_run_timestamp,
        metrics.membership_last_run_duration,
        metrics.membership_last_run_observed,
        metrics.membership_last_run_matched,
        metrics.membership_last_run_unmatched,
        metrics.membership_last_run_partial,
        metrics.membership_last_run_matched_by_serial,
        metrics.membership_last_run_unresolved,
    ):
        gauge.clear()

    if audit is not None:
        metrics.audit_events_stored.set(audit.total)
        oldest = _epoch(audit.oldest_created_at)
        # An unlabelled gauge cannot be absent, so an empty trail reads "now": an age of 0.
        metrics.audit_oldest_event_timestamp.set(oldest if oldest is not None else time.time())

    if pruned is not None:
        metrics.servers_pruned_24h.clear()
        for provider, count in pruned.items():
            metrics.servers_pruned_24h.labels(source_provider=provider).set(count)

    for row in snapshot.by_provider:
        provider = row.source_provider or "unknown"
        metrics.servers_total.labels(source_provider=provider).set(row.total)
        metrics.servers_stale.labels(source_provider=provider).set(row.stale)
        metrics.servers_unreachable.labels(source_provider=provider).set(row.unreachable)
        metrics.servers_partial.labels(source_provider=provider).set(row.partial)
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

    for policy_key, count in snapshot.by_policy.items():
        metrics.policy_active.labels(policy_key=policy_key).set(count)

    metrics.servers_in_maintenance.set(snapshot.in_maintenance)
    metrics.duplicate_name_groups.set(snapshot.duplicate_name_groups)
    metrics.duplicate_name_servers.set(snapshot.duplicate_name_servers)
    metrics.openshift_name_mismatch_servers.set(snapshot.openshift_name_mismatches)
    metrics.openshift_contested_servers.set(snapshot.openshift_contested)

    for manager in managers:
        run = manager.last_run
        if run is None:
            continue
        labels = {"source_provider": manager.type.value}
        metrics.collector_last_run_timestamp.labels(**labels).set(run.finished_at.timestamp())
        metrics.collector_last_run_duration.labels(**labels).set(run.duration_seconds)
        metrics.collector_last_run_fetched.labels(**labels).set(run.servers_fetched)
        metrics.collector_last_run_unchanged.labels(**labels).set(run.servers_unchanged)
        metrics.collector_last_run_ingest_errors.labels(**labels).set(run.ingest_errors)
        metrics.collector_last_run_collection_errors.labels(**labels).set(run.collection_errors)
        metrics.collector_last_run_partial.labels(**labels).set(int(run.partial))

    for run in membership_runs:
        labels = {"kind": run.kind, "reported_by": run.reported_by}
        metrics.membership_last_run_timestamp.labels(**labels).set(run.finished_at.timestamp())
        metrics.membership_last_run_duration.labels(**labels).set(run.duration_seconds)
        metrics.membership_last_run_observed.labels(**labels).set(run.observed)
        metrics.membership_last_run_matched.labels(**labels).set(run.matched)
        metrics.membership_last_run_unmatched.labels(**labels).set(run.unmatched)
        metrics.membership_last_run_partial.labels(**labels).set(int(run.partial))
        metrics.membership_last_run_matched_by_serial.labels(**labels).set(run.matched_by_serial)
        metrics.membership_last_run_unresolved.labels(**labels).set(run.unresolved)


class FleetGaugeRefresher:
    """Throttles fleet-gauge refreshes so concurrent scrapes share one query."""

    def __init__(
        self,
        repo: FleetSnapshotSource,
        managers: ManagerSource,
        membership_runs: MembershipRunSource,
        *,
        pruned: PrunedSource | None = None,
        audit: AuditStatsSource | None = None,
        stale_after_seconds: int,
        min_interval_seconds: float,
    ) -> None:
        """
        Bind the refresher to its three sources and two knobs.

        Args:
            repo (FleetSnapshotSource): Where the fleet snapshot is read from.
            managers (ManagerSource): Where the collectors' run records are.
            membership_runs (MembershipRunSource): Where the membership
                jobs' run records are.
            pruned (PrunedSource | None): Audit source for the pruned gauge;
                `None` disables it.
            audit (AuditStatsSource | None): Audit source for the audit-trail
                size and oldest-event gauges; `None` disables them.
            stale_after_seconds (int): Age past which a server is stale.
            min_interval_seconds (float): Shortest gap between two queries.
        """
        self._repo = repo
        self._managers = managers
        self._membership_runs = membership_runs
        self._pruned = pruned
        self._audit = audit
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
                managers = await self._managers.list_all()
                membership_runs = await self._membership_runs.list_all()
                pruned = (
                    await self._pruned.count_by_provider_since(
                        PRUNED_EVENT_TYPE, utcnow() - timedelta(hours=24)
                    )
                    if self._pruned
                    else None
                )
                audit = await self._audit.audit_stats() if self._audit else None
            except Exception as exc:
                metrics.fleet_snapshot_failures_total.inc()
                logger.warning("metrics.fleet_snapshot_failed", error=str(exc))
                return False
            apply_snapshot(snapshot, managers, membership_runs, pruned, audit)
            self._last_refresh = now
            return True
