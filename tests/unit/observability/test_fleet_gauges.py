"""The fleet-gauge refresher: throttling, failure handling, label hygiene."""

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client import REGISTRY

from app.domain.ports.repository import ClusterSnapshotRow, FleetSnapshot, ProviderSnapshotRow
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
                last_seen_at="2026-09-12T10:00:00Z",
            )
        ],
        "by_cluster": [
            ClusterSnapshotRow(
                cluster_name="hc-tlv-01", held=4, last_reported_at="2026-09-12T09:30:00Z"
            )
        ],
        "by_health": {"HEALTHY": 7, "CRITICAL": 3},
        "in_maintenance": 2,
    }
    base.update(overrides)
    return FleetSnapshot(**base)


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
    apply_snapshot(_snapshot())

    assert _value("server_scan_servers", source_provider="OPENMANAGE") == 10
    assert _value("server_scan_servers_stale", source_provider="OPENMANAGE") == 3
    assert _value("server_scan_servers_unreachable", source_provider="OPENMANAGE") == 1
    seen = _value("server_scan_collector_last_seen_timestamp_seconds", source_provider="OPENMANAGE")
    assert seen == pytest.approx(1789207200.0)  # 2026-09-12T10:00:00Z
    assert _value("server_scan_cluster_servers_held", cluster="hc-tlv-01") == 4
    assert _value("server_scan_servers_by_health", severity="CRITICAL") == 3
    assert _value("server_scan_servers_in_maintenance") == 2


def test_apply_snapshot_drops_label_sets_that_vanished() -> None:
    """A retired collector or cluster must not keep reporting its last count."""
    apply_snapshot(_snapshot())
    apply_snapshot(_snapshot(by_provider=[], by_cluster=[], by_health={}))

    assert _value("server_scan_servers", source_provider="OPENMANAGE") is None
    assert _value("server_scan_cluster_servers_held", cluster="hc-tlv-01") is None


def test_a_never_seen_collector_has_no_timestamp_sample() -> None:
    """Zero would read as 1970 and page someone; absence is the honest value."""
    row = ProviderSnapshotRow(
        source_provider="ONEVIEW", total=1, stale=1, unreachable=0, last_seen_at=None
    )
    apply_snapshot(_snapshot(by_provider=[row]))

    assert _value("server_scan_servers_stale", source_provider="ONEVIEW") == 1
    assert (
        _value("server_scan_collector_last_seen_timestamp_seconds", source_provider="ONEVIEW")
        is None
    )


async def test_refresher_queries_once_per_interval() -> None:
    repo = _Repo(_snapshot())
    refresher = FleetGaugeRefresher(repo, stale_after_seconds=60, min_interval_seconds=3600)

    assert await refresher.maybe_refresh() is True
    assert await refresher.maybe_refresh() is False
    assert await refresher.maybe_refresh() is False
    assert repo.calls == 1


async def test_refresher_keeps_old_values_when_the_query_fails() -> None:
    apply_snapshot(_snapshot())
    before = metrics.fleet_snapshot_failures_total._value.get()
    refresher = FleetGaugeRefresher(
        _Repo(RuntimeError("mongo down")), stale_after_seconds=60, min_interval_seconds=0
    )

    assert await refresher.maybe_refresh() is False
    assert _value("server_scan_servers", source_provider="OPENMANAGE") == 10
    assert metrics.fleet_snapshot_failures_total._value.get() == before + 1
