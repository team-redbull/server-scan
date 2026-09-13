"""`reachable=False`: a server a provider knows about but could not reach.

Added 2026-09-10 alongside `OpenManageProvider._unreachable_server` — see
docs/dell-collectors.md's "Collection flow" update. The property under
test is the same one `test_ingest_partial_reads.py` covers for individual
fields, applied to a whole collection attempt: an unreachable run must not
blank a server's last-known hardware, and must leave a clear, durable
"currently unreachable" signal a health-blind field can't fake.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.application.services.ingest import IngestService
from app.domain.models.server import Server
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

SITES = site_catalog("")

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


class _OneShotProvider(ServerInventoryProvider):
    """Yields exactly the `ProviderServer`s it is handed."""

    provider_type = "test"

    def __init__(self, *servers: ProviderServer) -> None:
        super().__init__()
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
        sites=SITES,
        server_repo=MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
    )


def _reachable(serial: str) -> ProviderServer:
    """A normal, successfully-collected server."""
    return ProviderServer(
        external_id="redfish://10.20.30.50/redfish/v1/Systems/1",
        vendor="dell",
        name="ocp4-prod-tlv-worker-09",
        model="PowerEdge R650",
        serial=serial,
        cpu_sockets=2,
        cpu_cores=64,
        memory_total_bytes=512 * 1024**3,
    )


def _unreachable(serial: str) -> ProviderServer:
    """As `OpenManageProvider._unreachable_server` builds one: identity
    only, every optional field left at its `None` default.
    """
    return ProviderServer(
        external_id="ome-unreachable:10.20.30.50",
        vendor="dell",
        name="ocp4-prod-tlv-worker-09",
        serial=serial,
        reachable=False,
    )


async def _stored(mongo: MongoClientHolder, serial_normalized: str) -> Server:
    """
    Fetch the one server stored under a normalized serial.

    Args:
        mongo (MongoClientHolder): The test database connection.
        serial_normalized (str): `identity.serial_normalized` to look up.

    Returns:
        Server: The matching document.
    """
    repo = MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": serial_normalized},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    return page.items[0]


async def test_a_never_reached_server_is_recorded_unreachable_with_no_data(
    mongo_holder: MongoClientHolder,
) -> None:
    """The case that started this: OME knows the profile, its iDRAC has
    never once answered. There is nothing to carry forward, so the server
    simply appears unreachable with no hardware yet.
    """
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_unreachable("SN-NEVER-REACHED")))

    server = await _stored(mongo_holder, "sn-never-reached")
    assert server.reachable is False
    assert server.unreachable_since is not None
    assert server.last_seen_at is None
    assert server.hardware.cpu.sockets == 0


async def test_a_known_server_that_goes_unreachable_keeps_its_hardware(
    mongo_holder: MongoClientHolder,
) -> None:
    """The regression this whole feature exists to avoid: a transient miss
    must not wipe a server operators may still need to read.
    """
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_reachable("SN-GOES-DOWN")))
    first_seen = (await _stored(mongo_holder, "sn-goes-down")).last_seen_at

    await service.ingest(_OneShotProvider(_unreachable("SN-GOES-DOWN")))

    server = await _stored(mongo_holder, "sn-goes-down")
    assert server.reachable is False
    assert server.unreachable_since is not None
    assert server.hardware.cpu.sockets == 2
    assert server.hardware.memory.total_bytes == 512 * 1024**3
    # Not bumped: an unreachable run did not actually see this server.
    assert server.last_seen_at == first_seen


async def test_unreachable_since_does_not_reset_on_a_second_consecutive_miss(
    mongo_holder: MongoClientHolder,
) -> None:
    """So the UI can show how long a server has been down, not just that
    it currently is.
    """
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_unreachable("SN-STAYS-DOWN")))
    first = (await _stored(mongo_holder, "sn-stays-down")).unreachable_since

    await service.ingest(_OneShotProvider(_unreachable("SN-STAYS-DOWN")))
    second = (await _stored(mongo_holder, "sn-stays-down")).unreachable_since

    assert first == second


async def test_a_server_that_recovers_clears_the_unreachable_flag(
    mongo_holder: MongoClientHolder,
) -> None:
    """Recovery is a real state, not just the absence of new misses."""
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_unreachable("SN-RECOVERS")))
    await service.ingest(_OneShotProvider(_reachable("SN-RECOVERS")))

    server = await _stored(mongo_holder, "sn-recovers")
    assert server.reachable is True
    assert server.unreachable_since is None
    assert server.last_seen_at is not None
