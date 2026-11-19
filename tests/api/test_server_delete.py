"""`DELETE /api/v1/servers/{id}` (docs/adr/0065): remove one server; one in use needs `force`."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from tests.api.test_auth import _auth_enabled_app
from tests.api.test_server_reservation import _installable_server

from app.config import get_settings
from app.domain.enums import OpenShiftState
from app.domain.models.audit_event import Role
from app.domain.models.maintenance import Maintenance
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.reservation import Reservation
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.client import RedisClientHolder
from app.main import create_app
from app.utils.timeutil import utcnow


@pytest.fixture
async def app_context() -> AsyncIterator[
    tuple[AsyncClient, MongoServerRepository, MongoClientHolder]
]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        redis: RedisClientHolder = app.state.redis
        for name in ("servers", "sites", "managers", "audit_events"):
            await mongo.db[name].delete_many({})
        with contextlib.suppress(Exception):
            await redis.client.flushdb()
        yield client, MongoServerRepository(mongo, cursor_secret=settings.cursor_secret), mongo
        for name in ("servers", "sites", "managers", "audit_events"):
            await mongo.db[name].delete_many({})


async def _events(mongo: MongoClientHolder, server_id: str) -> list[dict]:
    return (
        await mongo.db["audit_events"]
        .find({"server_id": server_id, "event_type": "SERVER_DELETED"})
        .to_list(10)
    )


async def test_a_free_server_is_deleted_and_audited(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
) -> None:
    client, repo, mongo = app_context
    server = await repo.upsert(_installable_server(1))

    resp = await client.delete(f"/api/v1/servers/{server.id}")

    assert resp.status_code == 204
    assert await repo.get_by_id(server.id) is None
    (event,) = await _events(mongo, server.id)
    assert event["data"]["forced"] is False
    assert event["data"]["overridden"] == []
    assert event["server_name"] == server.name
    assert event["server_serial"] == server.identity.serial


async def test_a_missing_server_is_a_404_and_a_second_delete_too(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
) -> None:
    client, repo, _ = app_context
    server = await repo.upsert(_installable_server(2))
    assert (await client.delete("/api/v1/servers/srv_nope")).status_code == 404
    assert (await client.delete(f"/api/v1/servers/{server.id}")).status_code == 204
    assert (await client.delete(f"/api/v1/servers/{server.id}")).status_code == 404


async def test_detail_and_list_caches_forget_a_deleted_server(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
) -> None:
    client, repo, _ = app_context
    server = await repo.upsert(_installable_server(3))
    assert (await client.get(f"/api/v1/servers/{server.id}")).status_code == 200
    assert (await client.get("/api/v1/servers/rows")).json()["items"]

    await client.delete(f"/api/v1/servers/{server.id}")

    assert (await client.get(f"/api/v1/servers/{server.id}")).status_code == 404
    assert (await client.get("/api/v1/servers?page_size=10")).json()["items"] == []
    assert (await client.get("/api/v1/servers/rows")).json()["items"] == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            {
                "openshift": OpenShiftLifecycle(
                    lifecycle_state=OpenShiftState.INSTALLED, cluster_name="hc-nyc-02"
                )
            },
            "hc-nyc-02",
        ),
        (
            {
                "openshift": OpenShiftLifecycle(
                    lifecycle_state=OpenShiftState.INSTALLED_TO_INVENTORY
                )
            },
            "INSTALLED_TO_INVENTORY",
        ),
        (
            {
                "reservation": Reservation(
                    holder="install-server",
                    mce_cluster="mce-nyc",
                    created_at=utcnow(),
                    expires_at=utcnow() + timedelta(hours=1),
                )
            },
            "reserved for an install (mce-nyc)",
        ),
        ({"maintenance": Maintenance(enabled=True, reason="disk swap")}, "in maintenance"),
    ],
)
async def test_a_server_in_use_is_refused_without_force_and_deleted_with_it(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
    change: dict,
    expected: str,
) -> None:
    client, repo, mongo = app_context
    server = _installable_server(4)
    for field, value in change.items():
        setattr(server, field, value)
    server = await repo.upsert(server)

    refused = await client.delete(f"/api/v1/servers/{server.id}")

    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "SERVER_DELETE_BLOCKED"
    assert any(expected in reason for reason in body["details"]["blockers"])
    assert await repo.get_by_id(server.id) is not None
    assert await _events(mongo, server.id) == []

    forced = await client.delete(f"/api/v1/servers/{server.id}?force=true")

    assert forced.status_code == 204
    assert await repo.get_by_id(server.id) is None
    (event,) = await _events(mongo, server.id)
    assert event["data"]["forced"] is True
    assert any(expected in reason for reason in event["data"]["overridden"])


async def test_every_blocker_is_listed_not_just_the_first(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
) -> None:
    client, repo, _ = app_context
    server = _installable_server(5)
    server.openshift = OpenShiftLifecycle(lifecycle_state=OpenShiftState.INSTALLED)
    server.maintenance = Maintenance(enabled=True)
    server = await repo.upsert(server)

    resp = await client.delete(f"/api/v1/servers/{server.id}")

    assert len(resp.json()["details"]["blockers"]) == 2


async def test_an_expired_reservation_does_not_block(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
) -> None:
    client, repo, _ = app_context
    past = utcnow() - timedelta(hours=2)
    server = await repo.upsert(
        _installable_server(
            6,
            reservation=Reservation(
                holder="install-server", created_at=past, expires_at=past + timedelta(hours=1)
            ),
        )
    )
    assert (await client.delete(f"/api/v1/servers/{server.id}")).status_code == 204


@pytest.mark.parametrize("role", [Role.VIEWER, Role.AUDITOR])
async def test_only_an_admin_may_delete(role: Role) -> None:
    app = _auth_enabled_app(role)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        await client.post("/api/v1/auth/login", json={"username": "u", "password": "x"})
        assert (await client.delete("/api/v1/servers/any")).status_code == 403
        assert (await client.delete("/api/v1/servers/any?force=true")).status_code == 403


async def test_a_reservation_taken_after_the_check_still_blocks_the_delete(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is evaluated on a snapshot; the delete must not outrun a write made since."""
    client, repo, mongo = app_context
    server = await repo.upsert(_installable_server(7))
    real_get = MongoServerRepository.get_by_id
    calls = 0

    async def get_then_reserve(self: MongoServerRepository, server_id: str):  # noqa: ANN202
        nonlocal calls
        snapshot = await real_get(self, server_id)
        calls += 1
        if calls == 1:
            await mongo.db["servers"].update_one(
                {"_id": server_id},
                {
                    "$set": {
                        "reservation.holder": "install-server",
                        "reservation.mce_cluster": "mce-race",
                        "reservation.created_at": utcnow().isoformat(),
                        "reservation.expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
                    },
                    "$inc": {"revision": 1},
                },
            )
        return snapshot

    monkeypatch.setattr(MongoServerRepository, "get_by_id", get_then_reserve)

    resp = await client.delete(f"/api/v1/servers/{server.id}")

    assert resp.status_code == 409
    assert any("mce-race" in b for b in resp.json()["details"]["blockers"])
    assert await real_get(repo, server.id) is not None
    assert await _events(mongo, server.id) == []


async def test_a_failed_audit_write_does_not_undo_the_delete(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoClientHolder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.services.audit_service import AuditService

    client, repo, _ = app_context
    server = await repo.upsert(_installable_server(8))

    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(AuditService, "record", boom)

    assert (await client.delete(f"/api/v1/servers/{server.id}")).status_code == 204
    assert await repo.get_by_id(server.id) is None
