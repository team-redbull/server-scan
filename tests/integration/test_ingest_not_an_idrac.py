"""A Dell profile whose OME target is not an iDRAC stays one server, not one per run (ADR-0037)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.application.services.ingest import IngestService
from app.domain.enums import UnreachableReason
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

pytestmark = pytest.mark.integration


class _Provider(ServerInventoryProvider):
    """Yields one server: a healthy read, or the not-an-iDRAC stub."""

    provider_type = "OPENMANAGE"

    def __init__(self, *, stub: bool, serial: str | None, manager: str = "mgr-1") -> None:
        super().__init__()
        self.stub = stub
        self.serial = serial
        self.manager = manager

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        if self.stub:
            yield ProviderServer(
                external_id="ome-not-an-idrac:ocp4-nyc-prod-worker-04",
                vendor="dell",
                name="ocp4-nyc-prod-worker-04",
                serial=self.serial,
                manager_id=self.manager,
                reachable=False,
                unreachable_reason=UnreachableReason.NOT_AN_IDRAC,
            )
        else:
            yield ProviderServer(
                external_id="ome://1",
                vendor="dell",
                name="ocp4-nyc-prod-worker-04",
                serial="SN-OME-GOOD",
                manager_id=self.manager,
                bmc_address_raw="idrac-virtualmedia://10.0.0.4/redfish/v1/Systems/System.Embedded.1",
                memory_total_bytes=1,
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


async def test_a_serial_less_stub_is_one_document_across_runs(
    mongo_holder: MongoClientHolder,
) -> None:
    service, _repo = _service(mongo_holder)
    for _ in range(3):
        await service.ingest(_Provider(stub=True, serial=None))

    assert await mongo_holder.db["servers"].count_documents({}) == 1


async def test_a_stub_is_adopted_when_ome_later_supplies_the_serial(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_Provider(stub=True, serial=None))
    await service.ingest(_Provider(stub=False, serial=None))

    assert await mongo_holder.db["servers"].count_documents({}) == 1
    server = await repo.find_one_by_name("ocp4-nyc-prod-worker-04", filters={})
    assert server is not None
    assert server.identity.serial_normalized == "sn-ome-good"
    assert server.reachable is True


async def test_a_stub_never_adopts_another_managers_server(
    mongo_holder: MongoClientHolder,
) -> None:
    service, _repo = _service(mongo_holder)
    await service.ingest(_Provider(stub=True, serial=None, manager="mgr-1"))
    await service.ingest(_Provider(stub=True, serial=None, manager="mgr-2"))

    assert await mongo_holder.db["servers"].count_documents({}) == 2


async def test_a_healthy_server_with_a_serial_is_not_overwritten_by_a_serial_less_stub(
    mongo_holder: MongoClientHolder,
) -> None:
    service, repo = _service(mongo_holder)
    await service.ingest(_Provider(stub=False, serial=None))
    await service.ingest(_Provider(stub=True, serial=None))

    docs = await mongo_holder.db["servers"].find({}).to_list(length=None)
    assert len(docs) == 2  # the stub is its own document; the serial-bearing one is untouched
    good = next(d for d in docs if d["identity"]["serial_normalized"])
    assert good["reachable"] is True
    server = await repo.find_one_by_name(
        "ocp4-nyc-prod-worker-04", filters={"identity.serial_normalized": ""}
    )
    assert server is None or server.reachable is False
