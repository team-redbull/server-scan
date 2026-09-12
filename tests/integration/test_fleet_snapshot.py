"""`MongoServerRepository.fleet_snapshot` against the live dev MongoDB.

The gauges in ADR-0029 are only as good as this aggregation, and its two
sharp edges — the ISO-string cutoff (ADR-0006) and "never seen counts as
stale" — are exactly what a fixture-sized unit test would fake away.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.enums import HealthSeverity, OpenShiftState, Vendor
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration


def _server(
    name: str,
    *,
    provider: str,
    seen_ago: timedelta | None,
    reachable: bool = True,
    health: HealthSeverity = HealthSeverity.HEALTHY,
    maintenance: bool = False,
    cluster: str | None = None,
    reported_ago: timedelta | None = None,
    unread: tuple[str, ...] = (),
    policies: tuple[str, ...] = (),
) -> Server:
    """
    Build one stored server with just the fields the snapshot reads.

    Args:
        name (str): Server name, also its serial.
        provider (str): The `source_provider` to store.
        seen_ago (timedelta | None): How long ago it was last seen, or
            `None` for never.
        reachable (bool): The stored `reachable` flag.
        health (HealthSeverity): The stored overall health.
        maintenance (bool): Whether maintenance is enabled.
        cluster (str | None): An OpenShift cluster holding it, if any.
        reported_ago (timedelta | None): How long ago that cluster reported.
        unread (tuple[str, ...]): `unread_fields` to store.
        policies (tuple[str, ...]): `health.active_policy_keys` to store.

    Returns:
        Server: A server ready to upsert.
    """
    now = utcnow()
    openshift = OpenShiftLifecycle()
    if cluster is not None:
        openshift = OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.INSTALLED,
            cluster_name=cluster,
            last_reported_at=now - (reported_ago or timedelta(0)),
        )
    return Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(vendor=Vendor.DELL, serial=name, serial_normalized=normalize_text(name)),
        source_provider=provider,
        health=Health(overall=health, active_policy_keys=list(policies)),
        maintenance=Maintenance(enabled=maintenance),
        unread_fields=list(unread),
        openshift=openshift,
        reachable=reachable,
        created_at=now,
        updated_at=now,
        last_seen_at=None if seen_ago is None else now - seen_ago,
    )


async def test_fleet_snapshot_counts_stale_unreachable_and_last_seen(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret="t")
    for server in (
        _server(
            "fresh-1",
            provider="OPENMANAGE",
            seen_ago=timedelta(hours=1),
            unread=("hardware.memory.modules",),
        ),
        _server(
            "fresh-2",
            provider="OPENMANAGE",
            seen_ago=timedelta(hours=2),
            unread=("hardware.memory.modules",),
        ),
        _server(
            "old-1",
            provider="OPENMANAGE",
            seen_ago=timedelta(days=3),
            reachable=False,
            unread=("hardware.memory.modules",),
        ),
        # Every OPENMANAGE server below carries `hardware.memory.modules`
        # unread: structural, so it must not count. `hardware.gpus` is
        # unread on one of them: a real read failure, so it must.
        _server(
            "part-1",
            provider="OPENMANAGE",
            seen_ago=timedelta(hours=1),
            unread=("hardware.memory.modules", "hardware.gpus"),
        ),
        _server(
            "never-1", provider="OPENMANAGE", seen_ago=None, unread=("hardware.memory.modules",)
        ),
        _server("other-1", provider="ONEVIEW", seen_ago=timedelta(minutes=5)),
    ):
        await repo.upsert(server)

    snapshot = await repo.fleet_snapshot(stale_before=utcnow() - timedelta(hours=12))
    rows = {row.source_provider: row for row in snapshot.by_provider}

    ome = rows["OPENMANAGE"]
    assert ome.total == 5
    assert ome.partial == 1
    # The 3-day-old one and the never-seen one; "never" is stale, not exempt.
    assert ome.stale == 2
    assert ome.unreachable == 1
    # Max, not min: one dead BMC must not drag the collector's own liveness.
    assert ome.last_seen_at is not None
    assert ome.last_seen_at.endswith("Z")
    assert rows["ONEVIEW"].stale == 0


async def test_fleet_snapshot_groups_clusters_health_and_maintenance(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret="t")
    for server in (
        _server("a", provider="FAKE", seen_ago=timedelta(0), cluster="hc-tlv-01"),
        _server(
            "b",
            provider="FAKE",
            seen_ago=timedelta(0),
            cluster="hc-tlv-01",
            reported_ago=timedelta(hours=5),
            health=HealthSeverity.CRITICAL,
            maintenance=True,
            policies=("power.failed_psu", "storage.os_disk_bad_critical"),
        ),
        _server(
            "c",
            provider="FAKE",
            seen_ago=timedelta(0),
            health=HealthSeverity.CRITICAL,
            policies=("power.failed_psu",),
        ),
    ):
        await repo.upsert(server)

    snapshot = await repo.fleet_snapshot(stale_before=utcnow() - timedelta(hours=12))

    [cluster] = snapshot.by_cluster
    assert cluster.cluster_name == "hc-tlv-01"
    assert cluster.held == 2
    # The newest report wins, so a lagging node cannot make the whole
    # cluster look stale.
    assert cluster.last_reported_at is not None
    assert snapshot.by_health == {"HEALTHY": 1, "CRITICAL": 2}
    assert snapshot.by_policy == {"power.failed_psu": 2, "storage.os_disk_bad_critical": 1}
    assert snapshot.in_maintenance == 1


async def test_fleet_snapshot_on_an_empty_fleet(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret="t")
    snapshot = await repo.fleet_snapshot(stale_before=utcnow())
    assert snapshot.by_provider == []
    assert snapshot.by_cluster == []
    assert snapshot.by_health == {}
    assert snapshot.by_policy == {}
    assert snapshot.in_maintenance == 0


async def test_fleet_snapshot_reads_documents_written_before_the_new_fields(
    mongo_holder: MongoClientHolder,
) -> None:
    """The stored-shape rule: `health.active_policy_keys` and `unread_fields`
    are absent on older documents, and `last_run` on older managers.
    """
    server = _server("old-shape", provider="OPENMANAGE", seen_ago=timedelta(0))
    doc = server.model_dump(by_alias=True, mode="json")
    del doc["health"]["active_policy_keys"]
    del doc["unread_fields"]
    await mongo_holder.db["servers"].insert_one(doc)
    await mongo_holder.db["managers"].insert_one(
        {
            "_id": "mgr_old",
            "name": "old",
            "type": "OPENMANAGE",
            "enabled": True,
            "audit": {
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "created_by": "x",
                "updated_by": "x",
                "revision": 1,
            },
        }
    )

    repo = MongoServerRepository(mongo_holder, cursor_secret="t")
    snapshot = await repo.fleet_snapshot(stale_before=utcnow() - timedelta(hours=12))
    [row] = snapshot.by_provider
    assert row.partial == 0
    assert snapshot.by_policy == {}

    from app.infrastructure.mongodb.manager_repository import MongoManagerRepository

    managers = await MongoManagerRepository(mongo_holder).list_all()
    assert [m.last_run for m in managers] == [None]
