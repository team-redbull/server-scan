"""A serial-less standalone-Redfish stub is matched by BMC host (ADR-0037 decision 5)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.application.services.ingest import IngestService
from app.domain.enums import UnreachableReason
from app.domain.models.server import Server
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import SERVERS_COLLECTION
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

pytestmark = pytest.mark.integration

_HOST = "10.9.8.7"
_ADDRESS = f"redfish-virtualmedia://{_HOST}/redfish/v1/Systems/1"


class _Provider(ServerInventoryProvider):
    """Yields exactly the records it is handed, as one collector type."""

    def __init__(self, provider_type: str, *servers: ProviderServer) -> None:
        super().__init__()
        self.provider_type = provider_type
        self._servers = servers

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        for server in self._servers:
            yield server


def _service(mongo: MongoClientHolder) -> IngestService:
    return IngestService(
        sites=site_catalog(""),
        server_repo=MongoServerRepository(mongo, cursor_secret="s"),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
    )


def _ok(serial: str) -> ProviderServer:
    return ProviderServer(
        external_id="rf-1",
        vendor="dell",
        name="bmc-a",
        model="PowerEdge R650",
        serial=serial,
        cpu_sockets=2,
        bmc_address_raw=_ADDRESS,
    )


def _stub(reason: UnreachableReason = UnreachableReason.TIMEOUT) -> ProviderServer:
    return ProviderServer(
        external_id="rf-1",
        vendor="standalone",
        name="bmc-a",
        reachable=False,
        unreachable_reason=reason,
        bmc_address_raw=_ADDRESS,
    )


async def _all(mongo: MongoClientHolder) -> list[Server]:
    docs = await mongo.db[SERVERS_COLLECTION].find({}).to_list(length=None)
    return [Server.model_validate(d) for d in docs]


async def _ingest(
    mongo: MongoClientHolder, ps: ProviderServer, provider_type: str = "REDFISH_STANDALONE"
) -> None:
    await _service(mongo).ingest(_Provider(provider_type, ps))


async def test_a_never_answered_host_is_one_document_across_runs(
    mongo_holder: MongoClientHolder,
) -> None:
    await _ingest(mongo_holder, _stub())
    await _ingest(mongo_holder, _stub(UnreachableReason.NETWORK_UNREACHABLE))

    servers = await _all(mongo_holder)
    assert len(servers) == 1
    assert servers[0].reachable is False
    assert servers[0].unreachable_reason is UnreachableReason.NETWORK_UNREACHABLE
    assert servers[0].revision == 2


async def test_a_known_server_that_fails_is_marked_unreachable_in_place(
    mongo_holder: MongoClientHolder,
) -> None:
    await _ingest(mongo_holder, _ok("SN-KNOWN"))
    before = (await _all(mongo_holder))[0]

    await _ingest(mongo_holder, _stub())

    servers = await _all(mongo_holder)
    assert len(servers) == 1
    after = servers[0]
    assert after.id == before.id
    assert after.reachable is False
    assert after.identity.serial == "SN-KNOWN"
    assert after.identity.vendor == before.identity.vendor
    assert after.model == "PowerEdge R650"
    assert after.hardware.cpu.sockets == 2
    assert after.last_seen_at == before.last_seen_at


async def test_a_stub_that_later_answers_gains_its_serial_not_a_twin(
    mongo_holder: MongoClientHolder,
) -> None:
    await _ingest(mongo_holder, _stub())
    stub = (await _all(mongo_holder))[0]

    await _ingest(mongo_holder, _ok("SN-LATE"))

    servers = await _all(mongo_holder)
    assert len(servers) == 1
    assert servers[0].id == stub.id
    assert servers[0].identity.serial_normalized == "sn-late"
    assert servers[0].reachable is True
    assert servers[0].identity.vendor.value == "dell"


async def test_the_match_is_scoped_to_standalone_redfish(
    mongo_holder: MongoClientHolder,
) -> None:
    await _ingest(mongo_holder, _stub(), provider_type="OPENMANAGE")
    await _ingest(mongo_holder, _stub(), provider_type="OPENMANAGE")

    assert len(await _all(mongo_holder)) == 2


async def test_several_documents_on_one_host_prefer_the_one_with_a_serial(
    mongo_holder: MongoClientHolder,
) -> None:
    await _ingest(mongo_holder, _stub())
    await _ingest(mongo_holder, _ok("SN-TWIN"))
    collection = mongo_holder.db[SERVERS_COLLECTION]
    serial_doc = (await _all(mongo_holder))[0]
    clone = serial_doc.model_dump(by_alias=True, mode="json")
    clone["_id"] = "srv_clone"
    clone["identity"]["serial_normalized"] = ""
    clone["identity"]["serial"] = None
    await collection.insert_one(clone)

    await _ingest(mongo_holder, _stub())

    by_id = {s.id: s for s in await _all(mongo_holder)}
    assert by_id[serial_doc.id].reachable is False
    assert by_id["srv_clone"].revision == serial_doc.revision
