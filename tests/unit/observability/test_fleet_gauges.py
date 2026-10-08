"""The fleet-gauge refresher: throttling, failure handling, label hygiene."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from prometheus_client import REGISTRY

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager, ManagerRun
from app.domain.models.openshift import MembershipRun
from app.domain.ports.repository import (
    ActorEventCount,
    AuditActorCounts,
    AuditStats,
    ClusterSnapshotRow,
    FleetSnapshot,
    ProviderSnapshotRow,
)
from app.observability import metrics
from app.observability.fleet_gauges import FleetGaugeRefresher, apply_snapshot


def _snapshot(**overrides: Any) -> FleetSnapshot:
    """
    A one-provider, one-cluster snapshot with sane defaults.

    Args:
        **overrides (Any): `FleetSnapshot` fields to replace.

    Returns:
        FleetSnapshot: The snapshot.
    """
    base: dict[str, Any] = {
        "by_provider": [
            ProviderSnapshotRow(
                source_provider="OPENMANAGE",
                total=10,
                stale=3,
                unreachable=1,
                partial=2,
                last_seen_at="2026-09-12T10:00:00Z",
            )
        ],
        "by_cluster": [
            ClusterSnapshotRow(
                cluster_name="hc-tlv-01", held=4, last_reported_at="2026-09-12T09:30:00Z"
            )
        ],
        "by_health": {"HEALTHY": 7, "CRITICAL": 3},
        "by_policy": {"power.failed_psu": 3},
        "in_maintenance": 2,
        "duplicate_name_groups": 1,
        "duplicate_name_servers": 2,
        "openshift_name_mismatches": 1,
        "openshift_contested": 2,
    }
    base.update(overrides)
    return FleetSnapshot(**base)


def _manager(run: ManagerRun | None) -> Manager:
    """
    A manager document carrying one run record, or none.

    Args:
        run (ManagerRun | None): The run to attach.

    Returns:
        Manager: The manager.
    """
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    return Manager(
        _id="mgr_ome",
        name="ome",
        type=ManagerType.OPENMANAGE,
        audit=AuditFields(created_at=now, updated_at=now),
        last_run=run,
    )


RUN = ManagerRun(
    started_at=datetime(2026, 9, 12, 11, 58, tzinfo=UTC),
    finished_at=datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
    duration_seconds=120.0,
    servers_fetched=800,
    servers_created=0,
    servers_updated=800,
    servers_unchanged=760,
    ingest_errors=0,
    collection_errors=3,
    partial=False,
)

MEMBERSHIP_RUN = MembershipRun(
    kind="nodes",
    reported_by="hc-tlv-01",
    finished_at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
    duration_seconds=5.0,
    observed=50,
    matched=48,
    unmatched=2,
    partial=True,
    matched_by_serial=3,
    unresolved=1,
)


class _Managers:
    """A manager source stub."""

    def __init__(self, managers: list[Manager]) -> None:
        """
        Serve a fixed list.

        Args:
            managers (list[Manager]): What `list_all` returns.
        """
        self.managers = managers

    async def list_all(self) -> list[Manager]:
        """
        Return the fixed list.

        Returns:
            list[Manager]: The managers.
        """
        return self.managers


class _MembershipRuns:
    """A membership run source stub."""

    def __init__(self, runs: list[MembershipRun]) -> None:
        """
        Serve a fixed list.

        Args:
            runs (list[MembershipRun]): What `list_all` returns.
        """
        self.runs = runs

    async def list_all(self) -> list[MembershipRun]:
        """
        Return the fixed list.

        Returns:
            list[MembershipRun]: The runs.
        """
        return self.runs


class _Repo:
    """A repository stub counting how often the snapshot was asked for."""

    def __init__(self, snapshot: FleetSnapshot | Exception) -> None:
        """
        Serve one snapshot, or raise, on every call.

        Args:
            snapshot (FleetSnapshot | Exception): What `fleet_snapshot` yields.
        """
        self.snapshot = snapshot
        self.calls = 0

    async def fleet_snapshot(self, *, stale_before: Any) -> FleetSnapshot:
        """
        Return the canned snapshot.

        Args:
            stale_before (Any): Ignored.

        Returns:
            FleetSnapshot: The canned value.

        Raises:
            Exception: Whatever the stub was built to raise.
        """
        self.calls += 1
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


def _value(name: str, **labels: str) -> float | None:
    """
    Read one sample from the default registry.

    Args:
        name (str): The metric name.
        **labels (str): The label set to read.

    Returns:
        float | None: The sample value, or `None` if absent.
    """
    return REGISTRY.get_sample_value(name, labels or None)


def test_apply_snapshot_sets_every_gauge() -> None:
    apply_snapshot(_snapshot(), [_manager(RUN)], [MEMBERSHIP_RUN])

    assert _value("server_scan_servers", source_provider="OPENMANAGE") == 10
    assert _value("server_scan_servers_stale", source_provider="OPENMANAGE") == 3
    assert _value("server_scan_servers_unreachable", source_provider="OPENMANAGE") == 1
    seen = _value("server_scan_collector_last_seen_timestamp_seconds", source_provider="OPENMANAGE")
    assert seen == pytest.approx(1789207200.0)  # 2026-09-12T10:00:00Z
    assert _value("server_scan_cluster_servers_held", cluster="hc-tlv-01") == 4
    assert _value("server_scan_servers_by_health", severity="CRITICAL") == 3
    assert _value("server_scan_servers_in_maintenance") == 2
    assert _value("server_scan_duplicate_name_groups") == 1
    assert _value("server_scan_duplicate_name_servers") == 2
    assert _value("server_scan_openshift_name_mismatch_servers") == 1
    assert _value("server_scan_openshift_contested_servers") == 2
    assert _value("server_scan_servers_partial", source_provider="OPENMANAGE") == 2
    assert _value("server_scan_policy_active", policy_key="power.failed_psu") == 3
    assert (
        _value("server_scan_collector_last_run_servers_fetched", source_provider="OPENMANAGE")
        == 800
    )
    assert (
        _value("server_scan_collector_last_run_duration_seconds", source_provider="OPENMANAGE")
        == 120
    )
    assert (
        _value("server_scan_collector_last_run_servers_unchanged", source_provider="OPENMANAGE")
        == 760
    )
    assert _value("server_scan_collector_last_run_partial", source_provider="OPENMANAGE") == 0
    assert _value(
        "server_scan_collector_last_run_timestamp_seconds", source_provider="OPENMANAGE"
    ) == pytest.approx(RUN.finished_at.timestamp())
    assert (
        _value("server_scan_membership_last_run_unmatched", kind="nodes", reported_by="hc-tlv-01")
        == 2
    )
    assert (
        _value("server_scan_membership_last_run_partial", kind="nodes", reported_by="hc-tlv-01")
        == 1
    )
    assert _value(
        "server_scan_membership_last_run_timestamp_seconds", kind="nodes", reported_by="hc-tlv-01"
    ) == pytest.approx(MEMBERSHIP_RUN.finished_at.timestamp())
    assert (
        _value(
            "server_scan_membership_last_run_matched_by_serial",
            kind="nodes",
            reported_by="hc-tlv-01",
        )
        == 3
    )
    assert (
        _value("server_scan_membership_last_run_unresolved", kind="nodes", reported_by="hc-tlv-01")
        == 1
    )


def test_apply_snapshot_drops_label_sets_that_vanished() -> None:
    """A retired collector or cluster must not keep reporting its last count."""
    apply_snapshot(_snapshot(), [_manager(RUN)], [MEMBERSHIP_RUN])
    apply_snapshot(_snapshot(by_provider=[], by_cluster=[], by_health={}, by_policy={}), [], [])

    assert _value("server_scan_servers", source_provider="OPENMANAGE") is None
    assert _value("server_scan_cluster_servers_held", cluster="hc-tlv-01") is None
    assert _value("server_scan_policy_active", policy_key="power.failed_psu") is None
    assert _value("server_scan_collector_last_run_partial", source_provider="OPENMANAGE") is None
    assert (
        _value("server_scan_membership_last_run_unmatched", kind="nodes", reported_by="hc-tlv-01")
        is None
    )


def test_a_never_seen_collector_has_no_timestamp_sample() -> None:
    """Zero would read as 1970 and page someone; absence is the honest value."""
    row = ProviderSnapshotRow(
        source_provider="ONEVIEW", total=1, stale=1, unreachable=0, partial=0, last_seen_at=None
    )
    apply_snapshot(_snapshot(by_provider=[row]), [_manager(None)], [])

    assert _value("server_scan_servers_stale", source_provider="ONEVIEW") == 1
    assert (
        _value("server_scan_collector_last_seen_timestamp_seconds", source_provider="ONEVIEW")
        is None
    )
    # A manager that has never recorded a run likewise has no run samples.
    assert (
        _value("server_scan_collector_last_run_timestamp_seconds", source_provider="OPENMANAGE")
        is None
    )


async def test_refresher_queries_once_per_interval() -> None:
    repo = _Repo(_snapshot())
    refresher = FleetGaugeRefresher(
        repo,
        _Managers([]),
        _MembershipRuns([]),
        stale_after_seconds=60,
        min_interval_seconds=3600,
    )

    assert await refresher.maybe_refresh() is True
    assert await refresher.maybe_refresh() is False
    assert await refresher.maybe_refresh() is False
    assert repo.calls == 1


async def test_refresher_keeps_old_values_when_the_query_fails() -> None:
    apply_snapshot(_snapshot(), [], [])
    before = metrics.fleet_snapshot_failures_total._value.get()
    refresher = FleetGaugeRefresher(
        _Repo(RuntimeError("mongo down")),
        _Managers([]),
        _MembershipRuns([]),
        stale_after_seconds=60,
        min_interval_seconds=0,
    )

    assert await refresher.maybe_refresh() is False
    assert _value("server_scan_servers", source_provider="OPENMANAGE") == 10
    assert metrics.fleet_snapshot_failures_total._value.get() == before + 1


def test_pruned_gauge_follows_the_audit_counts_and_clears() -> None:
    """Providers with no prunes drop out; `None` leaves the gauge alone."""
    apply_snapshot(_snapshot(), [], [], {"ONEVIEW": 2})
    assert _value("server_scan_servers_pruned_24h", source_provider="ONEVIEW") == 2

    apply_snapshot(_snapshot(), [], [], None)
    assert _value("server_scan_servers_pruned_24h", source_provider="ONEVIEW") == 2

    apply_snapshot(_snapshot(), [], [], {})
    assert _value("server_scan_servers_pruned_24h", source_provider="ONEVIEW") is None


def test_audit_gauges_follow_the_trail_and_read_an_age_of_zero_when_it_is_empty() -> None:
    apply_snapshot(
        _snapshot(), [], [], audit=AuditStats(total=1200, oldest_created_at="2026-04-01T00:00:00Z")
    )
    assert _value("server_scan_audit_events") == 1200
    assert _value("server_scan_audit_oldest_event_timestamp_seconds") == pytest.approx(
        datetime(2026, 4, 1, tzinfo=UTC).timestamp()
    )

    apply_snapshot(_snapshot(), [], [], audit=None)
    assert _value("server_scan_audit_events") == 1200

    apply_snapshot(_snapshot(), [], [], audit=AuditStats(total=0, oldest_created_at=None))
    assert _value("server_scan_audit_events") == 0
    assert _value("server_scan_audit_oldest_event_timestamp_seconds") == pytest.approx(
        datetime.now(UTC).timestamp(), abs=5
    )


def test_a_run_recorded_before_the_unchanged_count_existed_reads_as_zero() -> None:
    stored = RUN.model_dump(mode="json")
    del stored["servers_unchanged"]
    assert ManagerRun.model_validate(stored).servers_unchanged == 0


class _Audit:
    """An audit source stub that serves one `AuditStats` and per-actor counts, or raises."""

    def __init__(
        self,
        result: AuditStats | Exception,
        actors: list[ActorEventCount] | Exception | None = None,
    ) -> None:
        self.result = result
        self.actors = actors or []
        self.actor_calls: list[bool] = []

    async def audit_stats(self) -> AuditStats:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def count_by_actor(self, since: datetime | None = None) -> list[ActorEventCount]:
        self.actor_calls.append(since is None)
        if isinstance(self.actors, Exception):
            raise self.actors
        return self.actors


async def test_a_failed_audit_query_is_a_counted_refresh_failure() -> None:
    apply_snapshot(_snapshot(), [], [], audit=AuditStats(total=5, oldest_created_at=None))
    before = metrics.fleet_snapshot_failures_total._value.get()
    refresher = FleetGaugeRefresher(
        _Repo(_snapshot()),
        _Managers([]),
        _MembershipRuns([]),
        audit=_Audit(RuntimeError("mongo down")),
        stale_after_seconds=60,
        min_interval_seconds=0,
    )

    assert await refresher.maybe_refresh() is False
    assert _value("server_scan_audit_events") == 5
    assert metrics.fleet_snapshot_failures_total._value.get() == before + 1


def test_a_vanished_manager_drops_its_unchanged_sample() -> None:
    apply_snapshot(_snapshot(), [_manager(RUN)], [])
    assert (
        _value("server_scan_collector_last_run_servers_unchanged", source_provider="OPENMANAGE")
        == 760
    )

    apply_snapshot(_snapshot(), [], [])
    assert (
        _value("server_scan_collector_last_run_servers_unchanged", source_provider="OPENMANAGE")
        is None
    )


def _counts(*rows: tuple[str, str, int]) -> list[ActorEventCount]:
    return [ActorEventCount(actor_type=t, actor=a, count=n) for t, a, n in rows]


def test_per_actor_gauges_follow_the_counts_and_drop_vanished_actors() -> None:
    both = AuditActorCounts(
        all_stored=_counts(("USER", "alice.cohen", 13), ("SYSTEM", "ingestion", 306)),
        last_24h=_counts(("USER", "alice.cohen", 2)),
    )
    apply_snapshot(_snapshot(), [], [], actors=both)
    assert _value("server_scan_audit_events_by_actor", actor_type="USER", actor="alice.cohen") == 13
    assert (
        _value("server_scan_audit_events_by_actor", actor_type="SYSTEM", actor="ingestion") == 306
    )
    assert (
        _value("server_scan_audit_events_by_actor_24h", actor_type="USER", actor="alice.cohen") == 2
    )

    apply_snapshot(_snapshot(), [], [], actors=None)
    assert _value("server_scan_audit_events_by_actor", actor_type="USER", actor="alice.cohen") == 13

    apply_snapshot(
        _snapshot(),
        [],
        [],
        actors=AuditActorCounts(all_stored=_counts(("USER", "bob.levi", 4)), last_24h=[]),
    )
    assert (
        _value("server_scan_audit_events_by_actor", actor_type="USER", actor="alice.cohen") is None
    )
    assert _value("server_scan_audit_events_by_actor", actor_type="USER", actor="bob.levi") == 4
    assert (
        _value("server_scan_audit_events_by_actor_24h", actor_type="USER", actor="alice.cohen")
        is None
    )


async def test_the_all_time_actor_count_refreshes_slower_than_the_24h_one() -> None:
    audit = _Audit(AuditStats(total=1, oldest_created_at=None), _counts(("USER", "alice.cohen", 1)))
    refresher = FleetGaugeRefresher(
        _Repo(_snapshot()),
        _Managers([]),
        _MembershipRuns([]),
        audit=audit,
        stale_after_seconds=60,
        min_interval_seconds=0,
    )

    assert await refresher.maybe_refresh() is True
    assert await refresher.maybe_refresh() is True
    assert audit.actor_calls == [False, True, False]


async def test_a_failing_actor_query_keeps_the_other_gauges_fresh() -> None:
    apply_snapshot(
        _snapshot(),
        [],
        [],
        actors=AuditActorCounts(all_stored=_counts(("USER", "alice.cohen", 13)), last_24h=[]),
    )
    before = metrics.fleet_snapshot_failures_total._value.get()
    refresher = FleetGaugeRefresher(
        _Repo(_snapshot(in_maintenance=7)),
        _Managers([]),
        _MembershipRuns([]),
        audit=_Audit(AuditStats(total=1, oldest_created_at=None), RuntimeError("slow")),
        stale_after_seconds=60,
        min_interval_seconds=0,
    )

    assert await refresher.maybe_refresh() is True
    assert _value("server_scan_servers_in_maintenance") == 7
    assert _value("server_scan_audit_events_by_actor", actor_type="USER", actor="alice.cohen") == 13
    assert metrics.fleet_snapshot_failures_total._value.get() == before


async def test_the_24h_counts_survive_when_only_the_all_time_query_fails() -> None:
    class _TotalsFail(_Audit):
        async def count_by_actor(self, since: datetime | None = None) -> list[ActorEventCount]:
            if since is None:
                raise RuntimeError("scan too slow")
            return _counts(("USER", "alice.cohen", 2))

    refresher = FleetGaugeRefresher(
        _Repo(_snapshot()),
        _Managers([]),
        _MembershipRuns([]),
        audit=_TotalsFail(AuditStats(total=1, oldest_created_at=None)),
        stale_after_seconds=60,
        min_interval_seconds=0,
    )

    assert await refresher.maybe_refresh() is True
    assert (
        _value("server_scan_audit_events_by_actor_24h", actor_type="USER", actor="alice.cohen") == 2
    )
