"""`KubeVirtProvider` against an in-process facade (docs/adr/0058)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.kubevirt_fixture import ROOT, SYSTEMS, KubeVirtFixture, Vm, kubevirt_tree

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.provider import ProviderServer, ServerIdentity
from app.infrastructure.providers.kubevirt.client import KubeVirtClient
from app.infrastructure.providers.kubevirt.provider import KubeVirtProvider
from app.infrastructure.providers.redfish.client import (
    RedfishAuthError,
    RedfishProtocolError,
    RedfishUnreachableError,
)

pytestmark = pytest.mark.unit

_KV01 = Vm(name="ocp-tlv-kv-01", serial="KV01-SERIAL-0001", cpu_model="Icelake-Server-v2")
_KV02 = Vm(name="ocp-tlv-kv-02", serial=None, uuid="22222222-2222-2222-2222-222222222222")
_KV03 = Vm(
    name="ocp-tlv-kv-03",
    power="Off",
    serial="33333333-3333-3333-3333-333333333333",
    nics=[("default", "02:5c:00:00:00:03")],
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(self: object, attempt: int, retry_after: str | None) -> None:
        return None

    monkeypatch.setattr(KubeVirtClient, "_backoff", instant)


@pytest.fixture
def facade() -> Iterator[KubeVirtFixture]:
    with KubeVirtFixture(resources=kubevirt_tree(_KV01, _KV02, _KV03)) as fixture:
        yield fixture


def _manager() -> Manager:
    return Manager(
        _id="mgr_kubevirt",
        name="kubevirt",
        type=ManagerType.KUBEVIRT,
        endpoint="https://kv.apps.example",
        enabled=True,
        audit=AuditFields.new(),
    )


def _provider(url: str, **overrides: Any) -> KubeVirtProvider:
    kwargs: dict[str, Any] = {
        "manager": _manager(),
        "url": url,
        "username": "scan",
        "password": "secret",
        "verify_tls": True,
        "ca_bundle": "",
        "connect_timeout": 2.0,
        "read_timeout": 5.0,
        "vm_budget_seconds": 10.0,
        "run_budget_seconds": 30.0,
        "concurrency": 4,
    }
    kwargs.update(overrides)
    return KubeVirtProvider(**kwargs)


async def _collect(provider: KubeVirtProvider) -> dict[str, ProviderServer]:
    return {s.name: s async for s in provider.collect()}


async def test_every_vm_is_collected(facade: KubeVirtFixture) -> None:
    provider = _provider(facade.url)
    servers = await _collect(provider)

    assert set(servers) == {"ocp-tlv-kv-01", "ocp-tlv-kv-02", "ocp-tlv-kv-03"}
    assert provider.collection_errors == ()
    assert servers["ocp-tlv-kv-01"].serial == "KV01-SERIAL-0001"
    assert servers["ocp-tlv-kv-01"].cpu_model == "Icelake-Server-v2"
    assert servers["ocp-tlv-kv-02"].serial == "vms.ocp-tlv-kv-02"
    assert [n.link_state for n in servers["ocp-tlv-kv-03"].nics] == ["UNKNOWN"]
    assert servers["ocp-tlv-kv-03"].nic_macs == ("02:5c:00:00:00:03",)
    assert all(s.storage_total_bytes == 1024**3 for s in servers.values())
    assert all(s.external_id.startswith("kubevirt://127.0.0.1:") for s in servers.values())


async def test_the_listing_is_read_once_and_processors_only_when_needed(
    facade: KubeVirtFixture,
) -> None:
    await _collect(_provider(facade.url))

    paths = facade.paths()
    assert paths.count(SYSTEMS) == 1
    assert not [p for p in paths if "/Processors" in p]
    assert all(authed for _, authed in facade.requests)
    # System + Memory(2) + Storage(2) + Drives(2) + EthernetInterfaces(2) per VM.
    assert len(paths) == 1 + 3 * 9


async def test_an_unread_topology_falls_back_to_the_processors(facade: KubeVirtFixture) -> None:
    vm = Vm(name="v", processor_summary=False, cpu_model="Skylake-Client")
    facade.resources = kubevirt_tree(vm)

    (server,) = (await _collect(_provider(facade.url))).values()

    assert server.cpu_model == "Skylake-Client"
    assert f"{vm.path}/Processors" in facade.paths()


async def test_each_vm_carries_the_configured_cluster_and_its_node(
    facade: KubeVirtFixture,
) -> None:
    facade.resources = kubevirt_tree(Vm(name="up", node="worker-1"), Vm(name="down", power="Off"))

    servers = await _collect(_provider(facade.url, cluster_name=" ocp4-tlv "))

    assert (servers["up"].vm_host_cluster, servers["up"].vm_host_node) == ("ocp4-tlv", "worker-1")
    assert (servers["down"].vm_host_cluster, servers["down"].vm_host_node) == ("ocp4-tlv", "")


async def test_an_empty_listing_yields_nothing_and_no_error(facade: KubeVirtFixture) -> None:
    facade.resources = kubevirt_tree()
    provider = _provider(facade.url)

    assert await _collect(provider) == {}
    assert provider.collection_errors == ()


class TestOneLoginAttemptPerRun:
    """The facade blocks an IP after 10 failed logins in 5 minutes (ADR-0058)."""

    async def test_health_check_spends_no_login(self, facade: KubeVirtFixture) -> None:
        await _provider(facade.url, password="wrong").health_check()

        assert facade.requests == [(ROOT, False)]
        assert facade.unauthorized_count() == 0

    async def test_a_wrong_password_fails_the_run_after_exactly_one_401(
        self, facade: KubeVirtFixture
    ) -> None:
        provider = _provider(facade.url, password="wrong")
        await provider.health_check()
        with pytest.raises(RedfishAuthError):
            await _collect(provider)

        assert facade.unauthorized_count() == 1

    async def test_a_401_mid_run_stops_the_run_without_hammering(
        self, facade: KubeVirtFixture
    ) -> None:
        facade.reject_after = 3
        provider = _provider(facade.url, concurrency=1)
        with pytest.raises(RedfishAuthError):
            await _collect(provider)

        assert facade.unauthorized_count() == 1


async def test_the_listing_failing_fails_the_run(facade: KubeVirtFixture) -> None:
    facade.faults[SYSTEMS] = 500
    with pytest.raises(RedfishProtocolError):
        await _collect(_provider(facade.url))


async def test_an_unreachable_facade_fails_the_run() -> None:
    with KubeVirtFixture() as fixture:
        url = fixture.url
    provider = _provider(url)
    with pytest.raises(RedfishUnreachableError):
        await provider.health_check()
    with pytest.raises(RedfishUnreachableError):
        await _collect(provider)


async def test_a_vm_deleted_mid_run_is_skipped_silently(facade: KubeVirtFixture) -> None:
    del facade.resources[_KV02.path]
    provider = _provider(facade.url)

    with capture_logs() as logs:
        servers = await _collect(provider)

    assert set(servers) == {"ocp-tlv-kv-01", "ocp-tlv-kv-03"}
    assert provider.collection_errors == ()
    assert any(e["event"] == "kubevirt.vm_vanished" for e in logs)


async def test_a_vm_that_errors_is_recorded_and_the_rest_still_arrive(
    facade: KubeVirtFixture,
) -> None:
    facade.faults[_KV02.path] = 500
    provider = _provider(facade.url)
    servers = await _collect(provider)

    assert set(servers) == {"ocp-tlv-kv-01", "ocp-tlv-kv-03"}
    (error,) = provider.collection_errors
    assert error.startswith("vms.ocp-tlv-kv-02: ")
    assert "500" in error


async def test_one_drive_failing_leaves_that_vms_storage_unread(facade: KubeVirtFixture) -> None:
    facade.faults[f"{_KV01.path}/Storage/1/Drives/scratch"] = 500
    provider = _provider(facade.url)
    servers = await _collect(provider)

    kv01 = servers["ocp-tlv-kv-01"]
    assert kv01.storage_drives is None
    assert kv01.storage_total_bytes is None
    assert kv01.storage_controllers is None
    assert servers["ocp-tlv-kv-02"].storage_drives is not None
    assert provider.collection_errors == ()


async def test_a_drive_that_vanished_leaves_storage_unread(facade: KubeVirtFixture) -> None:
    del facade.resources[f"{_KV01.path}/Storage/1/Drives/rootdisk"]
    servers = await _collect(_provider(facade.url))

    assert servers["ocp-tlv-kv-01"].storage_drives is None


@pytest.mark.parametrize(
    ("resource", "unread"),
    [
        ("EthernetInterfaces", ("nic_macs",)),
        ("EthernetInterfaces/default", ("nic_macs",)),
        ("Memory", ("memory_modules",)),
        ("Memory/1", ("memory_modules",)),
        ("Storage", ("storage_drives", "storage_controllers", "storage_total_bytes")),
        ("Storage/1", ("storage_drives", "storage_controllers", "storage_total_bytes")),
    ],
)
async def test_a_sub_resource_failing_is_unread_never_empty(
    facade: KubeVirtFixture, resource: str, unread: tuple[str, ...]
) -> None:
    facade.faults[f"{_KV01.path}/{resource}"] = 503
    provider = _provider(facade.url)
    servers = await _collect(provider)

    kv01 = servers["ocp-tlv-kv-01"]
    for field in unread:
        assert getattr(kv01, field) is None, field
    assert provider.collection_errors == ()


async def test_a_vm_over_its_budget_is_recorded(facade: KubeVirtFixture) -> None:
    # A VM is ~9 requests; 1 s leaves a loaded CI runner room, 0.1 s did not.
    facade.delays[_KV02.path] = 2.5
    provider = _provider(facade.url, vm_budget_seconds=1.0)
    servers = await _collect(provider)

    assert set(servers) == {"ocp-tlv-kv-01", "ocp-tlv-kv-03"}
    assert provider.collection_errors == ("vms.ocp-tlv-kv-02: exceeded its 1s budget",)


async def test_the_run_budget_stops_the_run_and_says_how_many_were_left(
    facade: KubeVirtFixture,
) -> None:
    for vm in (_KV01, _KV02, _KV03):
        facade.delays[vm.path] = 0.6
    provider = _provider(facade.url, run_budget_seconds=0.2, concurrency=1)
    servers = await _collect(provider)

    assert servers == {}
    (error,) = provider.collection_errors
    assert "run budget" in error
    assert "3 VM(s) not yet collected" in error


async def test_verification_disabled_is_logged(facade: KubeVirtFixture) -> None:
    with capture_logs() as logs:
        await _collect(_provider(facade.url, verify_tls=False))

    assert any(e["event"] == "kubevirt.tls_verification_disabled" for e in logs)


def test_a_bad_url_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="INVENTORY_KUBEVIRT_URL"):
        _provider("kv.apps.example")


class TestGetOne:
    """`GET /servers/available`'s recheck (ADR-0032) must equal the listed result."""

    async def test_equals_the_listed_server(self, facade: KubeVirtFixture) -> None:
        provider = _provider(facade.url)
        listed = await _collect(provider)
        for name, server in listed.items():
            fresh = await provider.get_one(ServerIdentity(external_id=server.external_id))
            assert fresh == server, name

    async def test_reads_only_that_vm(self, facade: KubeVirtFixture) -> None:
        provider = _provider(facade.url)
        external_id = f"kubevirt://kv.apps.example{_KV03.path}"
        fresh = await provider.get_one(ServerIdentity(external_id=external_id, name="ignored"))

        assert fresh is not None
        assert fresh.name == "ocp-tlv-kv-03"
        assert SYSTEMS not in facade.paths()
        assert all(p.startswith(_KV03.path) for p in facade.paths())

    async def test_a_renamed_route_still_reaches_the_configured_facade(
        self, facade: KubeVirtFixture
    ) -> None:
        provider = _provider(facade.url)
        fresh = await provider.get_one(
            ServerIdentity(external_id=f"kubevirt://old-route.example{_KV01.path}")
        )

        assert fresh is not None
        assert fresh.external_id.startswith("kubevirt://127.0.0.1:")

    @pytest.mark.parametrize(
        "external_id",
        [
            None,
            "",
            "redfish://kv/redfish/v1/Systems/vms.ocp-tlv-kv-01",
            "intersight/0123",
            "kubevirt://kv",
            "kubevirt://kv/redfish/v1/Systems/",
            "kubevirt://kv/redfish/v1/Systems/vms.ocp-tlv-kv-01/Memory",
            "kubevirt://kv/redfish/v1/Chassis/vms",
            "kubevirt://kv/redfish/v1/Systems/../../x",
        ],
    )
    async def test_a_foreign_or_malformed_id_is_none_with_no_request(
        self, facade: KubeVirtFixture, external_id: str | None
    ) -> None:
        provider = _provider(facade.url)

        assert await provider.get_one(ServerIdentity(external_id=external_id)) is None
        assert facade.requests == []

    async def test_a_deleted_vm_is_none(self, facade: KubeVirtFixture) -> None:
        provider = _provider(facade.url)
        external_id = "kubevirt://kv/redfish/v1/Systems/vms.gone"

        assert await provider.get_one(ServerIdentity(external_id=external_id)) is None

    async def test_a_rejected_login_is_none_after_one_401(self, facade: KubeVirtFixture) -> None:
        provider = _provider(facade.url, password="wrong")
        external_id = f"kubevirt://kv{_KV01.path}"

        with capture_logs() as logs:
            assert await provider.get_one(ServerIdentity(external_id=external_id)) is None
        assert facade.unauthorized_count() == 1
        assert any(e["event"] == "kubevirt.recheck_login_rejected" for e in logs)

    async def test_a_stalled_facade_is_none_within_the_vm_budget(
        self, facade: KubeVirtFixture
    ) -> None:
        facade.delays[_KV01.path] = 0.5
        provider = _provider(facade.url, vm_budget_seconds=0.1)

        assert (
            await provider.get_one(ServerIdentity(external_id=f"kubevirt://kv{_KV01.path}")) is None
        )
