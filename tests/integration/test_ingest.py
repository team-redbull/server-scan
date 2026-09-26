"""Integration test for the full ingest pipeline: the fake providers ->
`IngestService` -> `MongoServerRepository`/`MongoSiteRepository`/
`MongoManagerRepository`, against the live dev MongoDB.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.application.services.ingest import IngestService
from app.domain.enums import Vendor
from app.domain.models.reservation import Reservation
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import (
    COLLECTOR_TYPES,
    list_managers,
    list_sites,
)
from app.infrastructure.providers.fake.provider import fake_providers
from app.utils.timeutil import utcnow

SITES = site_catalog("")

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


def _service(mongo_holder: MongoClientHolder) -> IngestService:
    return IngestService(
        sites=SITES,
        server_repo=MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
    )


async def _ingest_all(
    service: IngestService, *, seed: int, count: int
) -> tuple[int, int, int, int]:
    """Run every collector's fake provider, as `tools/seed_inventory.py` does.

    Args:
        service (IngestService): The pipeline under test.
        seed (int): The generator seed.
        count (int): How many servers the whole fake fleet holds.

    Returns:
        tuple[int, int, int, int]: Summed `(fetched, created, updated, errors)`.
    """
    totals = [0, 0, 0, 0]
    for provider in fake_providers(seed=seed, count=count):
        summary = await service.ingest(provider, sites=list_sites(), managers=list_managers())
        for i, value in enumerate(
            (summary.fetched, summary.created, summary.updated, summary.errors)
        ):
            totals[i] += value
    return totals[0], totals[1], totals[2], totals[3]


async def test_ingest_creates_expected_number_of_servers(mongo_holder: MongoClientHolder) -> None:
    """Between them the two collectors own the whole fleet, exactly once."""
    service = _service(mongo_holder)

    fetched, created, updated, errors = await _ingest_all(service, seed=7, count=40)

    assert fetched == 40
    assert created == 40
    assert updated == 0
    assert errors == 0

    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    count = await server_repo.count({})
    assert count == 40


async def test_source_provider_names_the_collector_that_found_each_server(
    mongo_holder: MongoClientHolder,
) -> None:
    """Seeded data has to carry the same `source_provider` values a real
    run does, or the UI's source filter has nothing to match in dev.
    """
    service = _service(mongo_holder)
    await _ingest_all(service, seed=5, count=40)

    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await server_repo.list_page(
        filters={},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=100,
        with_count=False,
    )
    sources = {s.source_provider for s in page.items}
    # Derived, never restated: a hand-written copy of the collector set is
    # the drift that left OPENMANAGE unseeded and Dell missing from the
    # UI's Source filter.
    assert sources == {t.value for t in COLLECTOR_TYPES}
    for server in page.items:
        if server.identity.vendor == Vendor.CISCO:
            # The two Cisco collectors partition the Cisco fleet rather
            # than both claiming it — the same split the real Intersight
            # collector enforces by excluding ManagementMode == UCSM.
            assert server.source_provider in {"UCS_CENTRAL", "INTERSIGHT"}
        elif server.identity.vendor == Vendor.HP:
            # OneView owns the ProLiant fleet; nothing else does.
            assert server.source_provider == "ONEVIEW"
        elif server.identity.vendor == Vendor.DELL:
            # OpenManage owns the Dell fleet. Its hardware still arrives
            # over Redfish from each iDRAC (ADR-0020), so the two are
            # distinguishable only by vendor, never by `external_id`.
            assert server.source_provider == "OPENMANAGE"
        else:
            # What is left is a manufacturer this platform does not model,
            # reached at its own BMC because no aggregator owns it.
            assert server.source_provider == "REDFISH_STANDALONE"


async def test_reingest_same_seed_updates_not_duplicates(mongo_holder: MongoClientHolder) -> None:
    service = _service(mongo_holder)

    _, created, _, _ = await _ingest_all(service, seed=11, count=20)
    assert created == 20

    fetched, created, updated, errors = await _ingest_all(service, seed=11, count=20)
    assert fetched == 20
    assert created == 0
    assert updated == 20
    assert errors == 0

    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    assert await server_repo.count({}) == 20


async def test_reingest_carries_a_live_reservation_forward(
    mongo_holder: MongoClientHolder,
) -> None:
    """A collection run must not wipe an install lock.

    Ingest rebuilds every document, so a wiped reservation hands a machine that
    is mid-install back to the pool — ADR-0035, decision 2.
    """
    service = _service(mongo_holder)
    await _ingest_all(service, seed=41, count=3)
    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)

    (server_id,) = await server_repo.find_ids({}, limit=1)
    target = await server_repo.get_by_id(server_id)
    assert target is not None
    target.reservation = Reservation(
        holder="install-server",
        mce_cluster="ocp4-mce-alpha",
        infra_env="dell-r650-tlv-64c-1024gb",
        workflow_id="install-server-dell-r650-tlv-64c-1024gb",
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=2),
    )
    await server_repo.upsert(target)

    # The same seed, so the same machines are re-ingested and updated in place.
    await _ingest_all(service, seed=41, count=3)

    after = await server_repo.get_by_id(target.id)
    assert after is not None
    assert after.reservation.is_live() is True
    assert after.reservation.mce_cluster == "ocp4-mce-alpha"
    assert after.reservation.workflow_id == "install-server-dell-r650-tlv-64c-1024gb"


async def test_ingested_sites_and_managers_are_upserted(mongo_holder: MongoClientHolder) -> None:
    service = _service(mongo_holder)
    await _ingest_all(service, seed=3, count=10)

    site_repo = MongoSiteRepository(mongo_holder)
    manager_repo = MongoManagerRepository(mongo_holder)
    sites = await site_repo.list_all()
    managers = await manager_repo.list_all()

    assert len(sites) == len(list_sites())
    assert len(managers) == len(list_managers())


async def test_ingested_servers_are_searchable_and_filterable(
    mongo_holder: MongoClientHolder,
) -> None:
    service = _service(mongo_holder)
    await _ingest_all(service, seed=99, count=60)

    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    dell_page = await server_repo.list_page(
        filters={"identity.vendor": Vendor.DELL.value},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=100,
        with_count=True,
    )
    assert dell_page.total_count is not None
    assert dell_page.total_count > 0
    assert all(s.identity.vendor == Vendor.DELL for s in dell_page.items)
