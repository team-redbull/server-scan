"""A server whose mapping failed is written as a stub that keeps its data (ADR-0062)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.application.services.ingest import IngestService
from app.domain.ports.provider import (
    ProviderAttachment,
    ProviderNic,
    ProviderServer,
    ServerIdentity,
    ServerInventoryProvider,
    failed_server,
)
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

pytestmark = pytest.mark.integration

_MEMORY = 512 * 1024**3


class _Provider(ServerInventoryProvider):
    """Yields one server: a healthy read, or the stub a failed mapping produces."""

    provider_type = "ONEVIEW"

    def __init__(self, *, fail: bool) -> None:
        super().__init__()
        self.fail = fail

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        if self.fail:
            yield failed_server(
                external_id="/rest/server-hardware/1",
                vendor="hp",
                name="ocp4-tlv-worker-01",
                serial="SN-HP-1",
                manager_id="mgr-1",
                detail="mapping hardware: KeyError: 'processorCount'",
            )
        else:
            yield ProviderServer(
                external_id="/rest/server-hardware/1",
                vendor="hp",
                name="ocp4-tlv-worker-01",
                serial="SN-HP-1",
                manager_id="mgr-1",
                memory_total_bytes=_MEMORY,
                cpu_sockets=2,
            )


def _service(holder: MongoClientHolder) -> tuple[IngestService, MongoServerRepository]:
    repo = MongoServerRepository(holder, cursor_secret="test-cursor-secret")
    service = IngestService(
        sites=site_catalog(""),
        server_repo=repo,
        site_repo=MongoSiteRepository(holder),
        manager_repo=MongoManagerRepository(holder),
    )
    return service, repo


async def test_a_failed_mapping_keeps_the_stored_hardware_and_states_the_cause(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_Provider(fail=False))
    await service.ingest(_Provider(fail=True))

    assert await mongo_holder.db["servers"].count_documents({}) == 1
    server = await repo.find_one_by_name("ocp4-tlv-worker-01", filters={})
    assert server is not None
    assert server.reachable is False
    assert server.unreachable_reason is not None
    assert server.unreachable_reason.value == "collection_error"
    assert server.unreachable_detail == "mapping hardware: KeyError: 'processorCount'"
    assert server.hardware.memory.total_bytes == _MEMORY


async def test_the_next_good_read_clears_the_cause(mongo_holder: MongoClientHolder) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_Provider(fail=True))
    await service.ingest(_Provider(fail=False))

    assert await mongo_holder.db["servers"].count_documents({}) == 1
    server = await repo.find_one_by_name("ocp4-tlv-worker-01", filters={})
    assert server is not None
    assert server.reachable is True
    assert server.unreachable_reason is None
    assert server.unreachable_detail is None


class _RichProvider(ServerInventoryProvider):
    """A fabric-attached VM with NICs and a host, then the stub a failed mapping leaves."""

    provider_type = "KUBEVIRT"

    def __init__(self, *, fail: bool) -> None:
        super().__init__()
        self.fail = fail

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        if self.fail:
            yield failed_server(
                external_id="vms.ocp-tlv-kv-01",
                vendor="kubevirt",
                name="ocp-tlv-kv-01",
                serial="SER-KV-1",
                manager_id="mgr-kv",
                detail="mapping system: TypeError: boom",
            )
            return
        yield ProviderServer(
            external_id="vms.ocp-tlv-kv-01",
            vendor="kubevirt",
            name="ocp-tlv-kv-01",
            serial="SER-KV-1",
            manager_id="mgr-kv",
            memory_total_bytes=_MEMORY,
            cpu_sockets=1,
            nic_macs=("aa:bb:cc:dd:ee:01",),
            nics=(
                ProviderNic(
                    name="eno1", mac="aa:bb:cc:dd:ee:01", speed_mbps=25_000, link_state="UP"
                ),
            ),
            attachments=(
                ProviderAttachment(
                    type="uplink",
                    provider="x",
                    fabric="A",
                    fabric_name="fab-a",
                    fabric_id="1",
                    fabric_model="m",
                    fabric_serial="FS1",
                    server_interface="eno1",
                    server_port="1",
                    fabric_port="1/1",
                    admin_state="UP",
                    oper_state="UP",
                    speed_mbps=25_000,
                ),
            ),
            vm_host_cluster="cluster-1",
            vm_host_node="node-1",
            bmc_address_raw="redfish-virtualmedia://10.1.2.3:443/redfish/v1/Systems/1",
            system_uuid="uuid-kv-1",
            bmc_mac="aa:bb:cc:00:00:01",
        )


async def test_a_stub_keeps_attachments_nics_and_the_vm_host(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_RichProvider(fail=False))
    await service.ingest(_RichProvider(fail=True))

    server = await repo.find_one_by_name("ocp-tlv-kv-01", filters={})
    assert server is not None
    assert server.reachable is False
    assert len(server.connectivity.attachments) == 1
    assert [nic.name for nic in server.network.interfaces] == ["eno1"]
    assert server.vm_host is not None
    assert server.vm_host.node_name == "node-1"
    assert server.vm_host.cluster_name == "cluster-1"


async def test_the_same_failure_twice_does_not_rewrite_the_document(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_RichProvider(fail=False))
    await service.ingest(_RichProvider(fail=True))
    first = await repo.find_one_by_name("ocp-tlv-kv-01", filters={})
    await service.ingest(_RichProvider(fail=True))
    second = await repo.find_one_by_name("ocp-tlv-kv-01", filters={})

    assert first is not None
    assert second is not None
    assert second.revision == first.revision
    assert second.updated_at == first.updated_at


_BMC = "redfish-virtualmedia://10.1.2.3:443/redfish/v1/Systems/1"


async def test_a_stub_keeps_the_stored_bmc_address_uuid_and_bmc_mac(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_RichProvider(fail=False))
    await service.ingest(_RichProvider(fail=True))

    server = await repo.find_one_by_name("ocp-tlv-kv-01", filters={})
    assert server is not None
    assert server.reachable is False
    assert server.network.bmc.address_raw == _BMC
    assert server.network.bmc.host == "10.1.2.3"
    assert server.network.bmc.mac == "aa:bb:cc:00:00:01"
    assert server.identity.system_uuid == "uuid-kv-1"
    assert server.vm_host is not None


class _OmProvider(ServerInventoryProvider):
    """An OPENMANAGE-style read, then the stub for the same Dell when its iDRAC fails."""

    provider_type = "OPENMANAGE"

    def __init__(self, *, fail: bool, detail: str = "x") -> None:
        super().__init__()
        self.fail = fail
        self.detail = detail

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        if self.fail:
            yield failed_server(
                external_id="ome-1",
                vendor="dell",
                name="ocp4-tlv-dell-01",
                serial="SVCTAG1",
                manager_id="mgr-om",
                detail=self.detail,
            )
            return
        yield ProviderServer(
            external_id="ome-1",
            vendor="dell",
            name="ocp4-tlv-dell-01",
            serial="SVCTAG1",
            manager_id="mgr-om",
            memory_total_bytes=_MEMORY,
            cpu_sockets=2,
            bmc_address_raw="10.9.8.7",
            system_uuid="uuid-dell-1",
        )


async def test_a_previously_reachable_server_keeps_its_uuid_through_an_unreachable_stub(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_OmProvider(fail=False))
    await service.ingest(_OmProvider(fail=True))

    assert await mongo_holder.db["servers"].count_documents({}) == 1
    server = await repo.find_one_by_name("ocp4-tlv-dell-01", filters={})
    assert server is not None
    assert server.reachable is False
    assert server.identity.system_uuid == "uuid-dell-1"
    assert server.network.bmc.host == "10.9.8.7"


async def test_ingest_redacts_a_secret_in_a_stubs_detail_before_storing_it(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(
        _OmProvider(fail=True, detail="mapping: InvalidURL: https://u:pw@bmc/x?token=abc")
    )

    server = await repo.find_one_by_name("ocp4-tlv-dell-01", filters={})
    assert server is not None
    assert server.unreachable_detail is not None
    assert "pw" not in server.unreachable_detail
    assert "abc" not in server.unreachable_detail
    assert "https://***@bmc/x" in server.unreachable_detail
    assert "pw" not in str(await mongo_holder.db["servers"].find_one({}))
