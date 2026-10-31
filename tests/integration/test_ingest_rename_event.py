"""A rename seen by ingest is audited as SERVER_RENAMED, once, with both names (ADR-0051)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.application.services.audit_service import AuditService
from app.application.services.ingest import IngestService
from app.domain.models.audit_event import EventType
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

pytestmark = pytest.mark.integration


class _Provider(ServerInventoryProvider):
    """Yields one server under the name it is given."""

    provider_type = "OPENMANAGE"

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        yield ProviderServer(
            external_id="ome://1", vendor="dell", name=self.name, serial="SN-RENAME-1"
        )


async def test_a_rename_is_audited_once_and_an_unchanged_name_is_not(
    mongo_holder: MongoClientHolder,
) -> None:
    audit_repo = MongoAuditEventRepository(mongo_holder)
    service = IngestService(
        sites=site_catalog(""),
        server_repo=MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret"),
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
        audit=AuditService(repo=audit_repo),
    )
    provider = _Provider("ocp4-tlv-worker-5tb")

    await service.ingest(provider)
    await service.ingest(provider)
    provider.name = "ocp4-tlv-worker-10tb"
    await service.ingest(provider)
    await service.ingest(provider)

    page = await audit_repo.list_page(event_type=EventType.SERVER_RENAMED.value, page_size=10)
    assert len(page.items) == 1
    event = page.items[0]
    assert event.data == {"name_from": "ocp4-tlv-worker-5tb", "name_to": "ocp4-tlv-worker-10tb"}
    assert event.server_name == "ocp4-tlv-worker-10tb"
