"""`IngestService` never overwrites a write another actor made while it was ingesting (ADR-0044)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.application.services.ingest import IngestService
from app.domain.models.maintenance import Maintenance
from app.domain.models.server import Server
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.gpu_catalog import GpuCatalog
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"
_SERIAL = "SN-CONFLICT-1"


class _OneShotProvider(ServerInventoryProvider):
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


class _RacingRepo(MongoServerRepository):
    """Lets another writer win the revision race on the first write ingest attempts."""

    def __init__(self, *args: object, races: int = 1, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        self.raced = 0
        self._races = races

    async def upsert_with_revision_check(self, server: Server, *, expected_revision: int) -> Server:
        if self.raced < self._races:
            self.raced += 1
            current = await self.get_by_id(server.id)
            assert current is not None
            stored_revision = current.revision
            current.maintenance = Maintenance(enabled=True, reason="admin, mid-ingest")
            current.revision = stored_revision + 1
            await super().upsert_with_revision_check(current, expected_revision=stored_revision)
        return await super().upsert_with_revision_check(server, expected_revision=expected_revision)


class _DuplicateOnInsertRepo(MongoServerRepository):
    """Another process inserts the same vendor+serial just before our insert lands."""

    raced = False

    async def upsert(self, server: Server) -> Server:
        if not self.raced:
            self.raced = True
            rival = server.model_copy(update={"id": f"{server.id}-rival"})
            await super().upsert(rival)
        return await super().upsert(server)


def _provider_server(model: str = "UCSB-B200-M5") -> ProviderServer:
    """The same server; a different `model` is a real content change, forcing a full write."""
    return ProviderServer(
        external_id="ucsm://domain-1/sys/chassis-1/blade-9",
        vendor="cisco",
        name="ocp4-prod-tlv-infra-09",
        serial=_SERIAL,
        model=model,
    )


def _service(mongo: MongoClientHolder, repo: MongoServerRepository) -> IngestService:
    return IngestService(
        sites=site_catalog(""),
        gpu_catalog=GpuCatalog.from_spec(""),
        server_repo=repo,
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
    )


async def test_a_maintenance_write_made_during_ingest_survives(
    mongo_holder: MongoClientHolder,
) -> None:
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})
    plain = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    await _service(mongo_holder, plain).ingest(_OneShotProvider(_provider_server()))

    racing = _RacingRepo(mongo_holder, cursor_secret=_CURSOR_SECRET)
    summary = await _service(mongo_holder, racing).ingest(
        _OneShotProvider(_provider_server("UCSB-B200-M6"))
    )

    page = await plain.list_page(
        filters={"identity.serial": _SERIAL},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    stored = page.items[0]
    assert racing.raced == 1
    assert summary.errors == 0
    assert stored.maintenance.enabled is True
    assert stored.maintenance.reason == "admin, mid-ingest"
    assert stored.revision == 3
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})


async def _stored(repo: MongoServerRepository) -> list[Server]:
    page = await repo.list_page(
        filters={"identity.serial": _SERIAL},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=10,
        with_count=False,
    )
    return list(page.items)


async def test_a_server_that_keeps_changing_is_an_error_not_an_overwrite(
    mongo_holder: MongoClientHolder,
) -> None:
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})
    plain = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    await _service(mongo_holder, plain).ingest(_OneShotProvider(_provider_server()))

    racing = _RacingRepo(mongo_holder, cursor_secret=_CURSOR_SECRET, races=3)
    summary = await _service(mongo_holder, racing).ingest(
        _OneShotProvider(_provider_server("UCSB-B200-M6"))
    )

    assert summary.errors == 1
    assert racing.raced == 3
    (stored,) = await _stored(plain)
    assert stored.maintenance.enabled is True
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})


async def test_a_concurrent_insert_of_the_same_serial_is_adopted_not_duplicated(
    mongo_holder: MongoClientHolder,
) -> None:
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})
    repo = _DuplicateOnInsertRepo(mongo_holder, cursor_secret=_CURSOR_SECRET)

    summary = await _service(mongo_holder, repo).ingest(_OneShotProvider(_provider_server()))

    assert summary.errors == 0
    assert repo.raced is True
    assert len(await _stored(repo)) == 1
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})
