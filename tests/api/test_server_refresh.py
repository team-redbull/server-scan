"""`POST /api/v1/servers/{id}/refresh` (docs/adr/0064): one server, read from its manager now."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncGenerator, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from tests.api.test_auth import _auth_enabled_app

import app.api.v1.servers as servers_module
from app.application.services.audit_service import AuditService
from app.application.services.ingest import IngestService
from app.config import get_settings
from app.domain.models.audit_event import Role
from app.domain.models.server import Server
from app.domain.ports.provider import (
    NotThisManagersServerError,
    ProviderServer,
    ServerIdentity,
    ServerInventoryProvider,
)
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.redis.client import RedisClientHolder
from app.main import create_app

_RECORD = ProviderServer(
    external_id="ome://refresh-1",
    vendor="dell",
    name="ocp4-refresh-worker-01",
    serial="SN-REFRESH-1",
    model="PowerEdge R650",
)


class _Provider(ServerInventoryProvider):
    """An `OPENMANAGE` stand-in whose `get_one` answers `record`, `None`, or raises `error`."""

    provider_type = "OPENMANAGE"

    def __init__(
        self, record: ProviderServer | None = _RECORD, error: Exception | None = None
    ) -> None:
        super().__init__()
        self.record = record
        self.error = error
        self.calls = 0

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.record

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        if self.record is not None:
            yield self.record


@pytest.fixture
async def app_context() -> AsyncIterator[tuple[AsyncClient, MongoClientHolder]]:
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        redis: RedisClientHolder = app.state.redis
        for name in ("servers", "managers", "audit_events"):
            await mongo.db[name].delete_many({})
        with contextlib.suppress(Exception):
            await redis.client.flushdb()
        yield client, mongo
        for name in ("servers", "managers", "audit_events"):
            await mongo.db[name].delete_many({})


async def _seed(mongo: MongoClientHolder, record: ProviderServer = _RECORD) -> Server:
    """Store `record` the way a collector run would, and return the stored server."""
    service = IngestService(
        sites=site_catalog(""),
        server_repo=MongoServerRepository(mongo, cursor_secret=get_settings().cursor_secret),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
        audit=AuditService(repo=MongoAuditEventRepository(mongo)),
    )
    return await service.ingest_one(record, provider_type="OPENMANAGE")


def _use(monkeypatch: pytest.MonkeyPatch, provider: ServerInventoryProvider) -> None:
    monkeypatch.setattr(
        servers_module,
        "build_provider_for_manager_type",
        lambda manager_type, settings: provider,
    )


async def test_a_refresh_writes_what_the_manager_reports_now_and_audits_it(
    app_context: tuple[AsyncClient, MongoClientHolder], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo = app_context
    stored = await _seed(mongo)
    _use(monkeypatch, _Provider(dataclasses.replace(_RECORD, model="PowerEdge R760")))
    await client.get(f"/api/v1/servers/{stored.id}")  # warm the detail cache

    resp = await client.post(f"/api/v1/servers/{stored.id}/refresh")

    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "PowerEdge R760"
    assert body["revision"] > stored.revision
    assert (await client.get(f"/api/v1/servers/{stored.id}")).json()["model"] == "PowerEdge R760"
    events = (await client.get("/api/v1/events", params={"event_type": "SERVER_REFRESHED"})).json()[
        "items"
    ]
    assert len(events) == 1
    assert events[0]["server_id"] == stored.id
    assert events[0]["data"] == {"manager_type": "OPENMANAGE", "changed": True}


async def test_an_unchanged_server_keeps_its_revision_and_is_marked_seen(
    app_context: tuple[AsyncClient, MongoClientHolder], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo = app_context
    stored = await _seed(mongo)
    _use(monkeypatch, _Provider())
    # The seed skipped classification and health; the first refresh adds them.
    first = (await client.post(f"/api/v1/servers/{stored.id}/refresh")).json()

    resp = await client.post(f"/api/v1/servers/{stored.id}/refresh")

    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == first["revision"]
    assert body["last_seen_at"] > first["last_seen_at"]
    events = (await client.get("/api/v1/events", params={"event_type": "SERVER_REFRESHED"})).json()[
        "items"
    ]
    assert [event["data"]["changed"] for event in events] == [False, True]


async def test_a_manager_not_connected_to_the_api_is_a_409_naming_it(
    app_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """No OpenManage connection is configured in tests, so the real factory refuses."""
    client, mongo = app_context
    stored = await _seed(mongo)

    resp = await client.post(f"/api/v1/servers/{stored.id}/refresh")

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "SERVER_REFRESH_UNSUPPORTED"
    assert body["details"]["manager_type"] == "OPENMANAGE"
    assert "run its collector job" in body["detail"]


async def test_a_server_the_manager_no_longer_reports_is_left_unchanged(
    app_context: tuple[AsyncClient, MongoClientHolder], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo = app_context
    stored = await _seed(mongo)
    _use(monkeypatch, _Provider(record=None))

    resp = await client.post(f"/api/v1/servers/{stored.id}/refresh")

    assert resp.status_code == 409
    assert resp.json()["code"] == "SERVER_NOT_AT_MANAGER"
    assert (await client.get(f"/api/v1/servers/{stored.id}")).json()["revision"] == stored.revision


async def test_another_managers_server_is_a_409(
    app_context: tuple[AsyncClient, MongoClientHolder], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo = app_context
    stored = await _seed(mongo)
    _use(monkeypatch, _Provider(error=NotThisManagersServerError("another facade")))

    resp = await client.post(f"/api/v1/servers/{stored.id}/refresh")

    assert resp.status_code == 409
    assert resp.json()["code"] == "SERVER_REFRESH_UNSUPPORTED"


async def test_a_vendor_failure_is_a_503_naming_the_manager(
    app_context: tuple[AsyncClient, MongoClientHolder], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo = app_context
    stored = await _seed(mongo)
    _use(monkeypatch, _Provider(error=TimeoutError("timed out after 15.0s")))

    resp = await client.post(f"/api/v1/servers/{stored.id}/refresh")

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert body["details"]["manager_type"] == "OPENMANAGE"
    assert "timed out after 15.0s" in body["detail"]
    events = (await client.get("/api/v1/events", params={"event_type": "SERVER_REFRESHED"})).json()[
        "items"
    ]
    assert events == []


async def test_an_unknown_server_is_a_404(
    app_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    client, _mongo = app_context

    resp = await client.post("/api/v1/servers/server_nope/refresh")

    assert resp.status_code == 404


async def test_a_server_with_no_collector_is_a_409(
    app_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    client, mongo = app_context
    stored = await _seed(mongo)
    await mongo.db["servers"].update_one({"_id": stored.id}, {"$set": {"source_provider": None}})

    resp = await client.post(f"/api/v1/servers/{stored.id}/refresh")

    assert resp.status_code == 409
    assert "not collected by a vendor manager" in resp.json()["detail"]


@pytest.mark.parametrize("role", [Role.VIEWER, Role.AUDITOR])
async def test_only_an_admin_may_refresh(role: Role) -> None:
    app = _auth_enabled_app(role)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        await client.post("/api/v1/auth/login", json={"username": "u", "password": "x"})
        assert (await client.post("/api/v1/servers/any/refresh")).status_code == 403
