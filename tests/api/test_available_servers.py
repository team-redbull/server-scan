"""API tests for `GET /api/v1/servers/available` — both lookup modes, param
validation, and the route-ordering trap the `/servers/available` route
comment describes. See docs/adr/0032-available-server-lookup-api.md.

No vendor credentials are configured in this test environment, so every
candidate's `source_provider` degrades to trusting the stored document
(ADR-0032, Decision 5) — exactly what lets these tests seed a server's
final health/state directly via `MongoServerRepository.upsert` and assert
on it, the same way `test_servers.py` does for `GET /servers`.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

import app.api.v1.servers as servers_module
from app.config import get_settings
from app.domain.enums import HealthSeverity, ManagerType, OpenShiftState, Vendor
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.network import BmcInfo, NetworkInfo, NetworkInterface
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.providers.fake.provider import FakeProvider
from app.infrastructure.redis.client import RedisClientHolder
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


def _make_server(
    index: int,
    *,
    name: str | None = None,
    health: HealthSeverity = HealthSeverity.HEALTHY,
    # The draw requires the network category to have been READ and passed, so
    # a fixture leaving it UNKNOWN is excluded — ADR-0032, 2026-09-26 update.
    network_health: HealthSeverity = HealthSeverity.HEALTHY,
    vendor: Vendor = Vendor.STANDALONE,
    source_provider: str | None = None,
    bmc_host: str | None = None,
    interfaces: tuple[NetworkInterface, ...] = (),
) -> Server:
    now = utcnow()
    nm = name if name is not None else f"api-avail-srv-{index:04d}"
    serial = f"AVAILAPI{index:06d}"
    return Server(
        _id=new_id("server"),
        name=nm,
        name_normalized=normalize_text(nm),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            nic_macs=[i.mac for i in interfaces if i.mac],
        ),
        network=NetworkInfo(
            bmc=BmcInfo(host=bmc_host, host_is_ip=bmc_host is not None),
            interfaces=list(interfaces),
        ),
        health=Health(overall=health, network=network_health),
        source_provider=source_provider,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )


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


async def test_name_mode_returns_a_one_item_list(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_make_server(0, name="ocp-avail-name-exact"))

    resp = await client.get("/api/v1/servers/available", params={"name": "OCP-AVAIL-NAME-EXACT"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "name"
    assert body["requested"] == 1
    assert body["returned"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "ocp-avail-name-exact"
    assert body["items"][0]["live_recheck_performed"] is False


async def test_name_mode_no_match_is_404(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers/available", params={"name": "no-such-server"})

    assert resp.status_code == 404
    assert resp.json()["code"] == "AVAILABLE_SERVER_NOT_FOUND"


async def test_a_server_whose_network_was_never_read_is_not_drawn(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The hole `health.overall` cannot close.

    `UNKNOWN` never raises a `max()` — ADR-0032, 2026-09-26 update.
    """
    client, repo = app_context
    await repo.upsert(
        _make_server(
            0,
            name="ocp-avail-unread-net",
            health=HealthSeverity.HEALTHY,
            network_health=HealthSeverity.UNKNOWN,
        )
    )

    resp = await client.get("/api/v1/servers/available", params={"pattern": "ocp-avail-unread-net"})

    assert resp.status_code == 404
    assert resp.json()["code"] == "AVAILABLE_SERVER_NOT_FOUND"


async def test_a_server_whose_network_was_read_and_passed_is_drawn(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(
        _make_server(0, name="ocp-avail-good-net", network_health=HealthSeverity.HEALTHY)
    )

    resp = await client.get("/api/v1/servers/available", params={"pattern": "ocp-avail-good-net"})

    assert resp.status_code == 200
    assert resp.json()["items"][0]["name"] == "ocp-avail-good-net"


async def test_pattern_mode_defaults_count_to_one(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    for i in range(3):
        await repo.upsert(_make_server(i, name=f"ocp-avail-pat-{i:02d}"))

    resp = await client.get("/api/v1/servers/available", params={"pattern": "ocp-avail-pat"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "pattern"
    assert body["requested"] == 1
    assert body["returned"] == 1
    assert len(body["items"]) == 1


async def test_pattern_mode_with_count_returns_a_list(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    for i in range(3):
        await repo.upsert(_make_server(i, name=f"ocp-avail-cnt-{i:02d}"))

    resp = await client.get(
        "/api/v1/servers/available", params={"pattern": "ocp-avail-cnt", "count": 2}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["requested"] == 2
    assert body["returned"] == 2
    assert len(body["items"]) == 2


async def test_pattern_mode_no_match_is_404(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get(
        "/api/v1/servers/available", params={"pattern": "no-such-server-pattern"}
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == "AVAILABLE_SERVER_NOT_FOUND"


async def test_neither_name_nor_pattern_is_400(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers/available")

    assert resp.status_code == 400
    assert resp.json()["code"] == "AVAILABLE_LOOKUP_CONFLICTING_PARAMS"


async def test_both_name_and_pattern_is_400(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers/available", params={"name": "x", "pattern": "y"})

    assert resp.status_code == 400
    assert resp.json()["code"] == "AVAILABLE_LOOKUP_CONFLICTING_PARAMS"


async def test_count_with_name_is_400(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers/available", params={"name": "x", "count": 2})

    assert resp.status_code == 400
    assert resp.json()["code"] == "AVAILABLE_LOOKUP_CONFLICTING_PARAMS"


async def test_count_above_max_is_422(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context
    settings = get_settings()

    resp = await client.get(
        "/api/v1/servers/available",
        params={"pattern": "ocp-anything", "count": settings.max_available_count + 1},
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "AVAILABLE_COUNT_TOO_LARGE"


async def test_invalid_vendor_is_422(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get(
        "/api/v1/servers/available",
        params={"pattern": "ocp-anything", "vendor": "not-a-vendor"},
    )

    assert resp.status_code == 422


async def test_source_provider_disambiguates_ucs_central_from_intersight(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(
        _make_server(
            0,
            name="ocp-avail-cisco-ucs",
            vendor=Vendor.CISCO,
            source_provider=ManagerType.UCS_CENTRAL.value,
        )
    )
    await repo.upsert(
        _make_server(
            1,
            name="ocp-avail-cisco-is",
            vendor=Vendor.CISCO,
            source_provider=ManagerType.INTERSIGHT.value,
        )
    )

    resp = await client.get(
        "/api/v1/servers/available",
        params={"pattern": "ocp-avail-cisco", "vendor": "cisco", "source_provider": "INTERSIGHT"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "ocp-avail-cisco-is"


async def test_route_does_not_collide_with_server_id(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """`/servers/available` must be matched before `/servers/{server_id}` —
    otherwise this 400s as a param conflict misrouted to a 404 for a
    server literally named "available".
    """
    client, _repo = app_context

    resp = await client.get("/api/v1/servers/available")

    assert resp.status_code == 400
    assert resp.json()["code"] == "AVAILABLE_LOOKUP_CONFLICTING_PARAMS"


async def test_item_carries_exactly_what_a_bmh_generator_needs(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The response is the BMH/NMState input set — MACs, per-interface OS
    names, a bare BMC host, the BMC-driver vendor — and none of `ServerDetail`'s
    hardware/health/classification payload (ADR-0032, 2026-09-13 update).
    """
    client, repo = app_context
    await repo.upsert(
        _make_server(
            0,
            name="ocp-dell-r650-five-128c-1024gb-5tb-del0001",
            vendor=Vendor.DELL,
            source_provider=ManagerType.OPENMANAGE.value,
            bmc_host="10.20.30.41",
            interfaces=(
                NetworkInterface(
                    name="NIC.Integrated.1-1-1", mac="00:00:5e:00:53:01", location="1/1/1"
                ),
                NetworkInterface(
                    name="NIC.Integrated.1-2-1", mac="00:00:5e:00:53:02", location="1/2/1"
                ),
            ),
        )
    )

    resp = await client.get(
        "/api/v1/servers/available", params={"name": "ocp-dell-r650-five-128c-1024gb-5tb-del0001"}
    )

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert set(item) == {
        "id",
        "name",
        "vendor",
        "source_provider",
        "bmc_vendor",
        "bmc",
        "nic_macs",
        "interfaces",
        "site_id",
        "health_overall",
        "live_recheck_performed",
    }
    assert item["bmc_vendor"] == "DELL"
    assert item["bmc"]["host"] == "10.20.30.41"
    assert item["nic_macs"] == ["00:00:5e:00:53:01", "00:00:5e:00:53:02"]
    assert [i["mac"] for i in item["interfaces"]] == item["nic_macs"]
    # Only the dev `.env.example` mapping is under test here: `NIC.Integrated.1` is
    # configured, so both ports resolve; a Slot the mapping lacks would be null.
    if get_settings().nic_os_names:
        assert [i["os_name"] for i in item["interfaces"]] == ["eno12399np0", "eno12409np1"]
    # Seeded straight into Mongo with no ingest and no live recheck, so the
    # site was never parsed from the name; the field is present, not derived.
    assert item["site_id"] is None
    assert "hardware" not in item


async def test_bmc_vendor_tells_intersight_from_ucs_within_cisco(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """bmhgen keys its `bmc.address` scheme on a fourth vendor value, `INTERSIGHT`,
    which `identity.vendor` alone cannot express.
    """
    client, repo = app_context
    await repo.upsert(
        _make_server(
            0,
            name="ocp-cisco-m6-bat-yam-ucs",
            vendor=Vendor.CISCO,
            source_provider=ManagerType.UCS_CENTRAL.value,
            interfaces=(NetworkInterface(name="eth0", mac="00:00:5e:00:53:10"),),
        )
    )
    await repo.upsert(
        _make_server(
            1,
            name="ocp-cisco-m6-bat-yam-is",
            vendor=Vendor.CISCO,
            source_provider=ManagerType.INTERSIGHT.value,
        )
    )

    ucs = (
        await client.get("/api/v1/servers/available", params={"name": "ocp-cisco-m6-bat-yam-ucs"})
    ).json()["items"][0]
    intersight = (
        await client.get("/api/v1/servers/available", params={"name": "ocp-cisco-m6-bat-yam-is"})
    ).json()["items"][0]

    assert ucs["bmc_vendor"] == "CISCO"
    assert intersight["bmc_vendor"] == "INTERSIGHT"
    # Cisco vNICs are named positionally from eno5 (`cisco_eno_names`).
    assert ucs["interfaces"][0]["os_name"] == "eno5"


async def test_a_vendor_manager_failure_is_a_503_naming_the_manager_not_a_500(
    app_context: tuple[AsyncClient, MongoServerRepository], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 15 s UCS Central `lsServer` timeout used to surface as an unexplained 500."""
    client, repo = app_context
    await repo.upsert(
        _make_server(
            0,
            name="ocp-ucs-timeout",
            vendor=Vendor.CISCO,
            source_provider=ManagerType.UCS_CENTRAL.value,
        )
    )

    class _TimingOut(FakeProvider):
        async def get_one(self, identity: object) -> None:
            raise TimeoutError("query_classid('lsServer') timed out after 15.0s")

    monkeypatch.setattr(
        servers_module,
        "build_provider_for_manager_type",
        lambda manager_type, settings: _TimingOut(
            seed=1, count=1, provider_type=ManagerType.UCS_CENTRAL.value
        ),
    )

    resp = await client.get("/api/v1/servers/available", params={"name": "ocp-ucs-timeout"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert "UCS_CENTRAL" in body["detail"]
    assert "timed out after 15.0s" in body["detail"]
    assert body["details"]["manager_type"] == "UCS_CENTRAL"


def _installed(server: Server, cluster: str = "ocp4-prod") -> Server:
    return server.model_copy(
        update={
            "openshift": OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name=cluster
            )
        }
    )


def _in_maintenance(server: Server, reason: str = "replacing PSU") -> Server:
    return server.model_copy(update={"maintenance": Maintenance(enabled=True, reason=reason)})


async def test_asking_for_an_installed_server_says_it_is_installed(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_installed(_make_server(0, name="ocp-already-installed"), "ocp4-tlv"))

    resp = await client.get("/api/v1/servers/available", params={"name": "ocp-already-installed"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "AVAILABLE_SERVER_NOT_FOUND"
    assert "already installed in cluster 'ocp4-tlv'" in body["detail"]
    assert body["details"]["reasons"] == ["INSTALLED"]


async def test_asking_for_a_server_in_maintenance_says_so_with_the_reason(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_in_maintenance(_make_server(0, name="ocp-in-maint"), "swapping DIMM"))

    resp = await client.get("/api/v1/servers/available", params={"name": "ocp-in-maint"})

    assert resp.status_code == 404
    assert "in maintenance (swapping DIMM)" in resp.json()["detail"]
    assert resp.json()["details"]["reasons"] == ["MAINTENANCE"]


async def test_a_server_in_maintenance_is_never_drawn_but_its_sibling_is(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_in_maintenance(_make_server(0, name="ocp-batch-a")))
    await repo.upsert(_make_server(1, name="ocp-batch-b"))

    resp = await client.get(
        "/api/v1/servers/available", params={"pattern": "ocp-batch-", "count": 5}
    )

    assert resp.status_code == 200
    assert [item["name"] for item in resp.json()["items"]] == ["ocp-batch-b"]


async def test_a_pattern_where_nothing_is_available_counts_each_reason(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_installed(_make_server(0, name="ocp-none-1")))
    await repo.upsert(_installed(_make_server(1, name="ocp-none-2")))
    await repo.upsert(_in_maintenance(_make_server(2, name="ocp-none-3")))

    resp = await client.get("/api/v1/servers/available", params={"pattern": "ocp-none-"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"] == (
        "3 servers match 'ocp-none-', but none is available: "
        "2 installed in a cluster, 1 in maintenance"
    )
    assert body["details"]["reasons"] == ["INSTALLED", "MAINTENANCE"]


async def test_a_pattern_matching_no_name_at_all_is_a_plain_404(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers/available", params={"pattern": "zzz-nope"})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "no server name matches 'zzz-nope'"
