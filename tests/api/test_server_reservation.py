"""The install lock: `POST`/`DELETE /api/v1/servers/{id}/reservation`.

Against a real app and the live dev Mongo, because the part worth testing is
what the STORE does. The mutual exclusion is a revision-checked write, and the
exclusion from a draw is a Mongo clause — neither is observable from a unit test
with a fake repository, and both have a failure mode with no symptom: a lock that
records itself, displays itself, and excludes nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.domain.enums import HealthSeverity, LinkState, OpenShiftState, Vendor
from app.domain.models.health import Health
from app.domain.models.network import BmcInfo, NetworkInfo, NetworkInterface
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.reservation import Reservation
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.client import RedisClientHolder
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

MCE_A = "ocp4-mce-alpha"
MCE_B = "ocp4-mce-beta"
INFRA_ENV = "dell-r650-tlv-64c-1024gb"


def _installable_server(index: int, *, reservation: Reservation | None = None) -> Server:
    """A server `/servers/available` would draw: AVAILABLE, HEALTHY, two UP NICs."""
    now = utcnow()
    name = f"ocp-{INFRA_ENV}-RES{index:05d}"
    serial = f"RESTEST{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=Vendor.DELL,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"res-test-uuid-{index:06d}",
            nic_macs=[f"aa:bb:cc:dd:{index:02x}:01", f"aa:bb:cc:dd:{index:02x}:02"],
        ),
        health=Health(overall=HealthSeverity.HEALTHY, network=HealthSeverity.HEALTHY),
        openshift=OpenShiftLifecycle(lifecycle_state=OpenShiftState.AVAILABLE),
        network=NetworkInfo(
            bmc=BmcInfo(host="10.11.1.229"),
            interfaces=[
                NetworkInterface(
                    name="NIC.Integrated.1-1-1",
                    mac=f"aa:bb:cc:dd:{index:02x}:01",
                    location="1/1/1",
                    link_state=LinkState.UP,
                ),
                NetworkInterface(
                    name="NIC.Integrated.1-2-1",
                    mac=f"aa:bb:cc:dd:{index:02x}:02",
                    location="1/2/1",
                    link_state=LinkState.UP,
                ),
            ],
        ),
        reservation=reservation or Reservation(),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


@pytest.fixture
async def app_context() -> AsyncIterator[tuple[AsyncClient, MongoServerRepository]]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        redis: RedisClientHolder = app.state.redis
        for name in ("servers", "sites", "managers"):
            await mongo.db[name].delete_many({})
        with contextlib.suppress(Exception):
            await redis.client.flushdb()
        repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        yield client, repo
        for name in ("servers", "sites", "managers"):
            await mongo.db[name].delete_many({})


def _reserve_body(**overrides: object) -> dict[str, object]:
    return {
        "holder": "install-server",
        "mce_cluster": MCE_A,
        "infra_env": INFRA_ENV,
        "namespace": "multicluster-engine",
        "workflow_id": "install-server-" + INFRA_ENV,
        **overrides,
    }


async def test_reserving_records_which_mce_is_installing(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The field the whole lock is legible by.

    Two MCEs drawing from one InfraEnv pool is the case this exists for, so
    "reserved" without naming the cluster answers half the question.
    """
    client, repo = app_context
    server = await repo.upsert(_installable_server(1))

    resp = await client.post(f"/api/v1/servers/{server.id}/reservation", json=_reserve_body())

    assert resp.status_code == 200
    stored = await repo.get_by_id(server.id)
    assert stored is not None
    assert stored.reservation.holder == "install-server"
    assert stored.reservation.mce_cluster == MCE_A
    assert stored.reservation.infra_env == INFRA_ENV
    assert stored.reservation.workflow_id == "install-server-" + INFRA_ENV
    assert stored.reservation.is_live() is True


async def test_a_second_mce_cannot_take_a_held_server(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The refusal this exists for, and it must SAY who holds it.

    A 409 that does not name the holding cluster leaves the loser unable to
    report anything useful about why it moved on.
    """
    client, repo = app_context
    server = await repo.upsert(_installable_server(2))
    first = await client.post(f"/api/v1/servers/{server.id}/reservation", json=_reserve_body())
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/servers/{server.id}/reservation",
        json=_reserve_body(mce_cluster=MCE_B, workflow_id="install-server-other"),
    )

    assert second.status_code == 409
    body = second.json()
    assert MCE_A in str(body)


async def test_the_same_run_extends_its_own_lock_rather_than_losing_it(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Temporal retries activities, so a claim must be idempotent for its holder.

    Without this a retried claim would look like a lost race and send the run
    off to a different machine it does not need.
    """
    client, repo = app_context
    server = await repo.upsert(_installable_server(3))
    body = _reserve_body()

    first = await client.post(f"/api/v1/servers/{server.id}/reservation", json=body)
    assert first.status_code == 200
    first_read = await repo.get_by_id(server.id)
    assert first_read is not None
    taken_at = first_read.reservation.created_at

    again = await client.post(f"/api/v1/servers/{server.id}/reservation", json=body)

    assert again.status_code == 200
    stored = await repo.get_by_id(server.id)
    assert stored is not None
    # created_at survives the extension: the record still says when the machine
    # was first taken, not when it was last renewed.
    assert stored.reservation.created_at == taken_at


async def test_a_reserved_server_is_not_drawn(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The point of the lock. A recorded reservation that excludes nothing is
    the failure mode with no symptom."""
    client, repo = app_context
    held = await repo.upsert(
        _installable_server(
            4,
            reservation=Reservation(
                holder="install-server",
                mce_cluster=MCE_A,
                infra_env=INFRA_ENV,
                created_at=utcnow(),
                expires_at=utcnow() + timedelta(hours=2),
            ),
        )
    )
    free = await repo.upsert(_installable_server(5))

    resp = await client.get(
        "/api/v1/servers/available",
        params={"pattern": f"^ocp-{INFRA_ENV}", "count": 5, "min_nic_macs": 2},
    )

    assert resp.status_code == 200
    drawn = {item["id"] for item in resp.json()["items"]}
    assert free.id in drawn
    assert held.id not in drawn


async def test_an_expired_reservation_does_not_withhold_the_server(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Expiry is what keeps a crashed run from costing the machine for good."""
    client, repo = app_context
    stale = await repo.upsert(
        _installable_server(
            6,
            reservation=Reservation(
                holder="install-server",
                mce_cluster=MCE_A,
                created_at=utcnow() - timedelta(hours=4),
                expires_at=utcnow() - timedelta(hours=2),
            ),
        )
    )

    resp = await client.get(
        "/api/v1/servers/available",
        params={"pattern": f"^ocp-{INFRA_ENV}", "count": 5, "min_nic_macs": 2},
    )

    assert resp.status_code == 200
    assert stale.id in {item["id"] for item in resp.json()["items"]}


async def test_a_lock_held_with_no_expiry_is_honoured(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """BSON orders null below every date, so a naive expiry comparison would read
    this as long expired — the one mistake that hands a machine to two clusters."""
    client, repo = app_context
    forever = await repo.upsert(
        _installable_server(
            7,
            reservation=Reservation(holder="operator", mce_cluster=MCE_A, created_at=utcnow()),
        )
    )

    resp = await client.get(
        "/api/v1/servers/available",
        params={"pattern": f"^ocp-{INFRA_ENV}", "count": 5, "min_nic_macs": 2},
    )

    drawn = set() if resp.status_code == 404 else {i["id"] for i in resp.json()["items"]}
    assert forever.id not in drawn


async def test_releasing_returns_the_server_to_the_pool(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    server = await repo.upsert(_installable_server(8))
    await client.post(f"/api/v1/servers/{server.id}/reservation", json=_reserve_body())

    resp = await client.request(
        "DELETE",
        f"/api/v1/servers/{server.id}/reservation",
        json={"holder": "install-server", "workflow_id": "install-server-" + INFRA_ENV},
    )

    assert resp.status_code == 200
    stored = await repo.get_by_id(server.id)
    assert stored is not None
    assert stored.reservation.is_live() is False
    assert stored.reservation.holder is None


async def test_releasing_an_unheld_server_is_success(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """A release retried after one that already worked must not fail."""
    client, repo = app_context
    server = await repo.upsert(_installable_server(9))

    resp = await client.request("DELETE", f"/api/v1/servers/{server.id}/reservation")

    assert resp.status_code == 200


async def test_one_run_cannot_release_another_runs_lock(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """A workflow's own release must never take a machine from a run that
    outlived it."""
    client, repo = app_context
    server = await repo.upsert(_installable_server(10))
    await client.post(f"/api/v1/servers/{server.id}/reservation", json=_reserve_body())

    resp = await client.request(
        "DELETE",
        f"/api/v1/servers/{server.id}/reservation",
        json={"holder": "install-server", "workflow_id": "some-other-run"},
    )

    assert resp.status_code == 409
    stored = await repo.get_by_id(server.id)
    assert stored is not None
    assert stored.reservation.is_live() is True


async def test_an_operator_can_clear_a_lock_whose_run_is_gone(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Sending no holder is the override: a run that crashed leaves a lock that
    outlives it, and waiting out the TTL should not be the only remedy."""
    client, repo = app_context
    server = await repo.upsert(_installable_server(11))
    await client.post(f"/api/v1/servers/{server.id}/reservation", json=_reserve_body())

    resp = await client.request("DELETE", f"/api/v1/servers/{server.id}/reservation")

    assert resp.status_code == 200
    stored = await repo.get_by_id(server.id)
    assert stored is not None
    assert stored.reservation.is_live() is False


async def test_the_inventory_row_says_which_mce_is_installing(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """So the fleet list answers "server X is installing to MCE Y" by itself,
    which is where the question is actually asked."""
    client, repo = app_context
    server = await repo.upsert(_installable_server(12))
    await client.post(f"/api/v1/servers/{server.id}/reservation", json=_reserve_body())

    resp = await client.get("/api/v1/servers/rows")

    assert resp.status_code == 200
    row = next(r for r in resp.json()["items"] if r["id"] == server.id)
    assert row["reservation"]["held"] is True
    assert row["reservation"]["mce_cluster"] == MCE_A
    assert row["reservation"]["infra_env"] == INFRA_ENV
    assert row["reservation"]["holder"] == "install-server"
