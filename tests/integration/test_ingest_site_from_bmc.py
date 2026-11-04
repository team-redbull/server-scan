"""A server whose name and profile say no site gets one from its BMC IP (ADR-0056)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.application.services.ingest import IngestService
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import SiteCatalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

pytestmark = pytest.mark.integration


class _Provider(ServerInventoryProvider):
    """Yields one server with the name and BMC address it is given."""

    provider_type = "OPENMANAGE"

    def __init__(self, name: str, bmc: str | None) -> None:
        super().__init__()
        self.name = name
        self.bmc = bmc

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        yield ProviderServer(
            external_id="ome://1",
            vendor="dell",
            name=self.name,
            serial="SN-BMC-SITE-1",
            bmc_address_raw=self.bmc,
        )


async def test_the_bmc_prefix_is_the_last_fallback_and_survives_an_unread_bmc(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret")
    service = IngestService(
        sites=SiteCatalog.from_spec("tlv:Tel Aviv,bat-yam:Bat Yam", "10=tlv,149=bat-yam"),
        server_repo=repo,
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
    )

    async def site_of(name: str, bmc: str | None) -> str | None:
        await service.ingest(_Provider(name, bmc))
        server = await repo.find_one_by_name(name.lower(), filters={})
        assert server is not None
        return server.site_id

    assert (
        await site_of("host-without-a-site", "idrac-virtualmedia://149.1.2.3/redfish") == "bat-yam"
    )
    assert await site_of("host-without-a-site", None) == "bat-yam"
    assert await site_of("ocp4-tlv-worker-01", "idrac-virtualmedia://149.1.2.3/redfish") == "tlv"


async def test_an_ip_derived_site_survives_repeated_unread_bmc_collections(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret")
    service = IngestService(
        sites=SiteCatalog.from_spec("tlv:Tel Aviv", "10=tlv"),
        server_repo=repo,
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
    )
    await service.ingest(_Provider("host-without-a-site", "idrac-virtualmedia://10.1.2.3/redfish"))
    for _ in range(3):
        await service.ingest(_Provider("host-without-a-site", None))
    server = await repo.find_one_by_name("host-without-a-site", filters={})
    assert server is not None
    assert server.site_id == "tlv"
