"""A real ingest resets at most N all-UNKNOWN Dell iDRACs per run, then the cooldown (ADR-0050)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest

from app.application.services.audit_service import AuditService
from app.application.services.bmc_reset_service import BmcResetService
from app.application.services.ingest import IngestService
from app.domain.models.audit_event import EventType
from app.domain.models.server import Server
from app.domain.ports.bmc_reset import BmcResetPolicy, BmcResetResult, ResetOutcome
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

pytestmark = pytest.mark.integration


class _Provider(ServerInventoryProvider):
    """Yields the given servers as if `OPENMANAGE` had collected them."""

    provider_type = "OPENMANAGE"

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


class _Resetter:
    """Counts the BMCs it is asked to restart."""

    def __init__(self) -> None:
        self.hosts: list[str | None] = []

    async def reset(self, server: Server, *, apply: bool) -> BmcResetResult:
        self.hosts.append(server.network.bmc.host)
        return BmcResetResult(ResetOutcome.REQUESTED, server.network.bmc.host)


def _unknown_dell(index: int) -> ProviderServer:
    """A Dell whose every drive reads UNKNOWN."""
    return ProviderServer(
        external_id=f"ome://{index}",
        vendor="dell",
        name=f"ocp4-tlv-worker-{index:02d}",
        serial=f"SN-RESET-{index}",
        bmc_address_raw=f"https://10.0.0.{index}",
        storage_total_bytes=1024**4,
        storage_drives=({"id": "d0", "health": "UNKNOWN"},),
    )


async def test_the_run_cap_bounds_the_resets_and_the_cooldown_stops_the_next_run(
    mongo_holder: MongoClientHolder,
) -> None:
    audit = AuditService(repo=MongoAuditEventRepository(mongo_holder))
    resetter = _Resetter()
    service = IngestService(
        sites=site_catalog(""),
        server_repo=MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret"),
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
        audit=audit,
        bmc_reset=BmcResetService(
            resetter=resetter,
            audit=audit,
            policy=BmcResetPolicy(report_only=False, cooldown=timedelta(hours=12), max_per_run=2),
        ),
    )
    provider = _Provider(*(_unknown_dell(i) for i in (1, 2, 3)))

    await service.ingest(provider)
    assert resetter.hosts == ["10.0.0.1", "10.0.0.2"]

    # The third server is next in line; the first two are inside the cooldown.
    await service.ingest(provider)
    assert resetter.hosts == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    await service.ingest(provider)
    assert len(resetter.hosts) == 3

    page = await MongoAuditEventRepository(mongo_holder).list_page(
        event_type=EventType.IDRAC_RESET.value, page_size=10
    )
    assert len(page.items) == 3


async def test_a_slow_reset_does_not_hold_up_the_rest_of_the_collection(
    mongo_holder: MongoClientHolder,
) -> None:
    gate = asyncio.Event()

    class _SlowResetter(_Resetter):
        async def reset(self, server: Server, *, apply: bool) -> BmcResetResult:
            await gate.wait()
            return await super().reset(server, apply=apply)

    audit = AuditService(repo=MongoAuditEventRepository(mongo_holder))
    resetter = _SlowResetter()
    service = IngestService(
        sites=site_catalog(""),
        server_repo=MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret"),
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
        audit=audit,
        bmc_reset=BmcResetService(
            resetter=resetter,
            audit=audit,
            policy=BmcResetPolicy(report_only=False, cooldown=timedelta(hours=12), max_per_run=5),
        ),
    )
    run = asyncio.create_task(service.ingest(_Provider(*(_unknown_dell(i) for i in (1, 2, 3)))))
    repo = MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret")

    async def stored() -> int:
        page = await repo.list_page(
            filters={}, search=None, sort="name", sort_desc=False, cursor=None,
            page_size=10, with_count=False,
        )  # fmt: skip
        return len(page.items)

    for _ in range(100):
        if await stored() == 3:
            break
        await asyncio.sleep(0.05)
    assert await stored() == 3
    assert not run.done()

    gate.set()
    summary = await run
    assert summary.fetched == 3
    assert len(resetter.hosts) == 3
