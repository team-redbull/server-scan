"""API tests for `GET /api/v1/servers/facets`.

The point of the endpoint is that its numbers describe the view the
operator is looking at, so every test here is about the counts moving
with the filters rather than about the aggregation itself.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.domain.enums import HealthSeverity, InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.client import RedisClientHolder
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


def _make_server(
    index: int,
    *,
    site_id: str | None,
    vendor: Vendor,
    source_provider: str,
    health: HealthSeverity = HealthSeverity.HEALTHY,
    installation_type: InstallationType = InstallationType.UPI,
) -> Server:
    """
    Build one persistable server.

    Args:
        index (int): Makes the serial and uuid unique within a test.
        site_id (str | None): The site to file it under.
        vendor (Vendor): Its manufacturer.
        source_provider (str): The collector that found it.
        health (HealthSeverity): Its overall health.
        installation_type (InstallationType): Its classification.

    Returns:
        Server: A server ready to `upsert`.
    """
    now = utcnow()
    name = f"facet-test-srv-{index:04d}"
    serial = f"FACET{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"facet-test-uuid-{index:06d}",
        ),
        site_id=site_id,
        source_provider=source_provider,
        classification=Classification(installation_type=installation_type),
        health=Health(overall=health),
        maintenance=Maintenance(enabled=False),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


@pytest.fixture
async def app_context() -> AsyncIterator[tuple[AsyncClient, MongoServerRepository]]:
    """
    A running app over a cleared servers collection and a flushed cache.

    Yields:
        tuple[AsyncClient, MongoServerRepository]: A client bound to the
            app, and a repository writing to the same database.
    """
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        redis: RedisClientHolder = app.state.redis
        await mongo.db["servers"].delete_many({})
        # These counts are cached by filter hash, which knows nothing
        # about test boundaries. Best-effort: Redis need not be up.
        with contextlib.suppress(Exception):
            await redis.client.flushdb()

        yield client, MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)

        await mongo.db["servers"].delete_many({})


async def _seed(repo: MongoServerRepository) -> None:
    """
    Two sites with different vendor mixes: tlv gets 3 Dell and 2 Cisco, nyc
    gets 1 Dell and 4 HPE, so a count ignoring the site filter reads 4 Dell for tlv.

    Args:
        repo (MongoServerRepository): Where to write them.
    """
    index = 0
    plan = (
        ("tlv", Vendor.DELL, "OPENMANAGE", 3),
        ("tlv", Vendor.CISCO, "INTERSIGHT", 2),
        ("nyc", Vendor.DELL, "OPENMANAGE", 1),
        ("nyc", Vendor.HP, "ONEVIEW", 4),
    )
    for site_id, vendor, source, count in plan:
        for _ in range(count):
            index += 1
            await repo.upsert(
                _make_server(index, site_id=site_id, vendor=vendor, source_provider=source)
            )


async def test_unfiltered_counts_cover_the_whole_fleet(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await _seed(repo)

    body = (await client.get("/api/v1/servers/facets")).json()

    assert body["total"] == 10
    assert body["vendor"] == {"dell": 4, "cisco": 2, "hp": 4}
    assert body["source_provider"] == {"OPENMANAGE": 4, "INTERSIGHT": 2, "ONEVIEW": 4}


async def test_counts_are_for_the_current_view_not_the_whole_fleet(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The whole feature. Inside tlv there are 3 Dell, not the fleet's 4 —
    a number that did not move with the site filter would be worse than no
    number, because it reads as one that did.
    """
    client, repo = app_context
    await _seed(repo)

    body = (await client.get("/api/v1/servers/facets?site_id=tlv")).json()

    assert body["total"] == 5
    assert body["vendor"] == {"dell": 3, "cisco": 2}
    assert body["source_provider"] == {"OPENMANAGE": 3, "INTERSIGHT": 2}


async def test_an_option_matching_nothing_here_is_absent_not_zero(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """HPE exists in the fleet but not in tlv, so it is not a key at all —
    the UI can then show it as unavailable rather than selectable.
    """
    client, repo = app_context
    await _seed(repo)

    body = (await client.get("/api/v1/servers/facets?site_id=tlv")).json()

    assert "hp" not in body["vendor"]
    assert "ONEVIEW" not in body["source_provider"]


async def test_filters_compose(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await _seed(repo)

    body = (await client.get("/api/v1/servers/facets?site_id=nyc&vendor=hp")).json()

    assert body["total"] == 4
    assert body["source_provider"] == {"ONEVIEW": 4}


async def test_search_narrows_the_counts_too(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The endpoint takes the same `?search=` as the list, so a caller can
    pass its whole query through unchanged.
    """
    client, repo = app_context
    await _seed(repo)

    body = (await client.get("/api/v1/servers/facets?search=facet-test-srv-0001")).json()

    assert body["total"] == 1


async def test_an_unknown_filter_is_rejected_here_as_on_the_list(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Sharing `build_filter_query` is what keeps the two endpoints from
    disagreeing about what a filter even is.
    """
    client, _ = app_context

    assert (await client.get("/api/v1/servers/facets?nonsense=1")).status_code == 400


async def test_facets_is_not_read_as_a_server_id(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """FastAPI matches routes in declaration order, so `/servers/facets`
    stops working the moment it is declared below `/servers/{server_id}`.
    """
    client, _ = app_context

    resp = await client.get("/api/v1/servers/facets")

    assert resp.status_code == 200
    assert "total" in resp.json()
