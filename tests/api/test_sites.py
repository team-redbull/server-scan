"""API tests for `GET /api/v1/sites` — the landing page's only query.

Data is inserted directly through `MongoServerRepository` rather than the
ingest pipeline, so these assert the aggregation/pivot contract the UI
reads, not classification behaviour.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

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
    site_id: str | None = None,
    vendor: Vendor = Vendor.DELL,
    health: HealthSeverity = HealthSeverity.UNKNOWN,
    installation_type: InstallationType = InstallationType.UNCLASSIFIED,
    maintenance_enabled: bool = False,
) -> Server:
    """Build one persistable server for these tests.

    Args:
        index (int): Makes the serial/uuid unique within a test.
        site_id (str | None): The site to file it under.
        vendor (Vendor): Its manufacturer.
        health (HealthSeverity): Its overall health.
        installation_type (InstallationType): Its classification.
        maintenance_enabled (bool): Whether it is in maintenance.

    Returns:
        Server: A server ready to `upsert`.
    """
    now = utcnow()
    name = f"sites-test-srv-{index:04d}"
    serial = f"SITESTEST{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"sites-test-uuid-{index:06d}",
        ),
        site_id=site_id,
        classification=Classification(installation_type=installation_type),
        health=Health(overall=health),
        maintenance=Maintenance(enabled=maintenance_enabled),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


@pytest.fixture
async def app_context() -> AsyncIterator[tuple[AsyncClient, MongoServerRepository]]:
    """A running app over a cleared servers collection and a flushed cache.

    Yields:
        tuple[AsyncClient, MongoServerRepository]: An HTTP client bound to
            the app, and a repository writing to the same database.
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
        # The stats cache key knows nothing about test boundaries, so one
        # test's cached response would otherwise be served to the next.
        # Best-effort: these tests do not require Redis to be up.
        with contextlib.suppress(Exception):
            await redis.client.flushdb()

        yield client, MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)

        await mongo.db["servers"].delete_many({})


def _site(body: dict[str, Any], site_id: str) -> dict[str, Any]:
    """Pull one site's record out of a `/sites` response body.

    Args:
        body (dict[str, Any]): The decoded response.
        site_id (str): The site to find.

    Returns:
        dict[str, Any]: That site's record.
    """
    return next(item for item in body["items"] if item["site_id"] == site_id)


async def test_every_site_reports_every_installation_type(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Zero-filled keys, so the UI never distinguishes "no UPI servers"
    from "the key is missing".
    """
    client, _ = app_context

    resp = await client.get("/api/v1/sites")

    assert resp.status_code == 200
    body = resp.json()
    for item in body["items"]:
        assert set(item["by_installation_type"]) == {
            "HOSTED_CLUSTER",
            "MCE",
            "UPI",
            "UNCLASSIFIED",
        }
        assert all(slice_["total"] == 0 for slice_ in item["by_installation_type"].values())

    assert body["fleet"]["total"] == 0
    assert set(body["fleet"]["by_installation_type"]) == {
        "HOSTED_CLUSTER",
        "MCE",
        "UPI",
        "UNCLASSIFIED",
    }


async def test_installation_type_slices_sum_to_the_site_total(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    for i in range(3):
        await repo.upsert(_make_server(i, site_id="tlv", installation_type=InstallationType.UPI))
    for i in range(3, 5):
        await repo.upsert(
            _make_server(i, site_id="tlv", installation_type=InstallationType.HOSTED_CLUSTER)
        )
    await repo.upsert(_make_server(5, site_id="tlv"))

    body = (await client.get("/api/v1/sites")).json()
    tlv = _site(body, "tlv")

    assert tlv["total"] == 6
    slices = tlv["by_installation_type"]
    assert slices["UPI"]["total"] == 3
    assert slices["HOSTED_CLUSTER"]["total"] == 2
    assert slices["UNCLASSIFIED"]["total"] == 1
    assert sum(s["total"] for s in slices.values()) == tlv["total"]


async def test_installation_type_slice_carries_health_vendor_and_maintenance(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Each slice reports the same breakdown a site does — that is what
    lets the fleet-wide UPI card render through the same component.
    """
    client, repo = app_context
    await repo.upsert(
        _make_server(
            0,
            site_id="nyc",
            vendor=Vendor.CISCO,
            health=HealthSeverity.CRITICAL,
            installation_type=InstallationType.UPI,
        )
    )
    await repo.upsert(
        _make_server(
            1,
            site_id="nyc",
            vendor=Vendor.DELL,
            health=HealthSeverity.HEALTHY,
            installation_type=InstallationType.UPI,
            maintenance_enabled=True,
        )
    )
    await repo.upsert(
        _make_server(
            2,
            site_id="nyc",
            vendor=Vendor.DELL,
            health=HealthSeverity.CRITICAL,
            installation_type=InstallationType.HOSTED_CLUSTER,
        )
    )

    body = (await client.get("/api/v1/sites")).json()
    upi = _site(body, "nyc")["by_installation_type"]["UPI"]

    assert upi["total"] == 2
    assert upi["by_health"]["CRITICAL"] == 1
    assert upi["by_health"]["HEALTHY"] == 1
    assert upi["in_maintenance"] == 1
    assert {v["vendor"]: v["count"] for v in upi["by_vendor"]} == {
        "dell": 1,
        "cisco": 1,
        "hp": 0,
        "standalone": 0,
    }


async def test_unassigned_bucket_slices_by_installation_type_too(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_make_server(0, installation_type=InstallationType.UPI))

    body = (await client.get("/api/v1/sites")).json()
    unassigned = _site(body, "unassigned")

    assert unassigned["total"] == 1
    assert unassigned["by_installation_type"]["UPI"]["total"] == 1


async def test_fleet_summary_sums_every_site_including_unassigned(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The fleet-wide total comes from the same aggregation pass as the per-site
    rows, so it must equal the hand sum of every `items` entry, unassigned included.
    """
    client, repo = app_context
    await repo.upsert(
        _make_server(
            0,
            site_id="tlv",
            vendor=Vendor.CISCO,
            health=HealthSeverity.CRITICAL,
            installation_type=InstallationType.UPI,
        )
    )
    await repo.upsert(
        _make_server(
            1,
            site_id="nyc",
            vendor=Vendor.DELL,
            health=HealthSeverity.HEALTHY,
            installation_type=InstallationType.HOSTED_CLUSTER,
            maintenance_enabled=True,
        )
    )
    await repo.upsert(_make_server(2, installation_type=InstallationType.UPI))

    body = (await client.get("/api/v1/sites")).json()
    fleet = body["fleet"]

    assert fleet["total"] == 3
    assert fleet["in_maintenance"] == 1
    assert fleet["by_health"]["CRITICAL"] == 1
    assert fleet["by_health"]["HEALTHY"] == 1
    # Server 2 (unassigned) defaults to Dell too, so this is 2, not 1.
    assert {v["vendor"]: v["count"] for v in fleet["by_vendor"]} == {
        "dell": 2,
        "cisco": 1,
        "hp": 0,
        "standalone": 0,
    }
    assert fleet["by_installation_type"]["UPI"]["total"] == 2
    assert fleet["by_installation_type"]["HOSTED_CLUSTER"]["total"] == 1
    assert fleet["by_installation_type"]["UNCLASSIFIED"]["total"] == 0

    assert fleet["total"] == sum(item["total"] for item in body["items"])
