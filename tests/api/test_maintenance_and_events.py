"""
API tests for maintenance and events: `PUT`/`DELETE .../maintenance`,
`GET /api/v1/events`, `GET /api/v1/servers/{id}/events`, and that every
audited mutation really produces the right `AuditEvent`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.domain.enums import HealthSeverity, InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.connectivity import Connectivity, ConnectivityFacts
from app.domain.models.health import Health
from app.domain.models.health_policy import EvidenceField, HealthPolicy, PolicyScope
from app.domain.models.server import Identity, Server
from app.domain.services.health.conditions import Condition
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


def _make_server(
    name: str,
    *,
    vendor: Vendor = Vendor.CISCO,
    index: int = 0,
    fabric_paths_down: int = 0,
    health: HealthSeverity = HealthSeverity.HEALTHY,
) -> Server:
    now = utcnow()
    serial = f"EVTAPI{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"evt-api-uuid-{index:06d}",
        ),
        classification=Classification(installation_type=InstallationType.UNCLASSIFIED),
        health=Health(overall=health, connectivity=health),
        connectivity=Connectivity(facts=ConnectivityFacts(fabric_paths_down=fabric_paths_down)),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


def _make_policy(
    name: str,
    *,
    fabric_paths_down_threshold: int = 2,
    severity: HealthSeverity = HealthSeverity.CRITICAL,
    priority: int = 200,
    scope: PolicyScope | None = None,
) -> HealthPolicy:
    now = utcnow()
    return HealthPolicy(
        id=new_id("health_policy"),
        name=name,
        policy_key="connectivity.fabric_paths_down",
        category="connectivity",
        severity=severity,
        condition=Condition(
            metric="connectivity.fabric_paths_down",
            operator="GTE",
            value=fabric_paths_down_threshold,
        ),
        evidence=[EvidenceField(key="down", metric="connectivity.fabric_paths_down")],
        message_template="{down} paths down",
        scope=scope or PolicyScope(),
        source="GLOBAL_CUSTOM",
        priority=priority,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def app_context() -> AsyncIterator[
    tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository]
]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        for name in (
            "servers",
            "sites",
            "managers",
            "classification_rules",
            "health_policies",
            "audit_events",
        ):
            await mongo.db[name].delete_many({})

        server_repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        policy_repo = MongoHealthPolicyRepository(mongo)
        yield client, server_repo, policy_repo

        for name in (
            "servers",
            "sites",
            "managers",
            "classification_rules",
            "health_policies",
            "audit_events",
        ):
            await mongo.db[name].delete_many({})


async def test_enable_maintenance_sets_fields_and_returns_server_detail(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-maint-1"))

    resp = await client.put(
        f"/api/v1/servers/{server.id}/maintenance",
        json={"reason": "disk replacement", "ticket": "INC-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["maintenance"]["enabled"] is True
    assert body["maintenance"]["reason"] == "disk replacement"
    assert body["maintenance"]["ticket"] == "INC-1"
    assert body["revision"] == server.revision + 1


async def test_the_maintenance_filter_reflects_a_write_immediately(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    """`?maintenance=true` must not serve a page cached before the write.

    A real operator report; ADR-0028 has it.
    """
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-maint-filter"))
    query = "/api/v1/servers?maintenance=true&page_size=50"

    # Prime the cache for this filter while the server is NOT in maintenance.
    assert (await client.get(query)).json()["items"] == []

    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "x"})
    listed = (await client.get(query)).json()["items"]
    assert [row["id"] for row in listed] == [server.id]

    await client.delete(f"/api/v1/servers/{server.id}/maintenance")
    assert (await client.get(query)).json()["items"] == []


async def test_a_maintenance_write_refreshes_the_rows_body(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    """The cached whole-fleet rows body is cleared on the same key family (ADR-0028)."""
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-maint-rows"))

    (before,) = (await client.get("/api/v1/servers/rows")).json()["items"]
    assert before["maintenance"]["enabled"] is False

    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "x"})
    (after,) = (await client.get("/api/v1/servers/rows")).json()["items"]
    assert after["maintenance"] == {"enabled": True, "reason": "x"}


async def test_enable_maintenance_records_maintenance_enabled_event(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-maint-2"))

    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "x"})

    events = (await client.get(f"/api/v1/servers/{server.id}/events")).json()["items"]
    assert any(e["event_type"] == "MAINTENANCE_ENABLED" for e in events)


async def test_updating_an_already_enabled_maintenance_records_updated_not_enabled(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-maint-3"))

    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "first"})
    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "second"})

    events = (await client.get(f"/api/v1/servers/{server.id}/events")).json()["items"]
    types = [e["event_type"] for e in events]
    assert types.count("MAINTENANCE_ENABLED") == 1
    assert types.count("MAINTENANCE_UPDATED") == 1


async def test_disable_maintenance_clears_it_and_records_event(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-maint-4"))
    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "x"})

    resp = await client.delete(f"/api/v1/servers/{server.id}/maintenance")

    assert resp.status_code == 200
    assert resp.json()["maintenance"]["enabled"] is False
    events = (await client.get(f"/api/v1/servers/{server.id}/events")).json()["items"]
    assert any(e["event_type"] == "MAINTENANCE_DISABLED" for e in events)


async def test_disabling_maintenance_that_was_never_enabled_records_no_event(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-maint-5"))

    resp = await client.delete(f"/api/v1/servers/{server.id}/maintenance")

    assert resp.status_code == 200
    events = (await client.get(f"/api/v1/servers/{server.id}/events")).json()["items"]
    assert events == []


async def test_maintenance_on_missing_server_returns_404(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, _repo, _policy_repo = app_context
    resp = await client.put("/api/v1/servers/srv_does_not_exist/maintenance", json={})
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_recalculate_health_records_health_status_changed_on_real_transition(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, policy_repo = app_context
    server = await repo.upsert(
        _make_server("srv-recalc-1", fabric_paths_down=2, health=HealthSeverity.UNKNOWN)
    )
    await policy_repo.upsert(_make_policy("critical-fabric-down"))

    resp = await client.post(f"/api/v1/servers/{server.id}/health/recalculate")

    assert resp.status_code == 200
    assert resp.json()["health"]["overall"] == "CRITICAL"
    events = (await client.get(f"/api/v1/servers/{server.id}/events")).json()["items"]
    changed = [e for e in events if e["event_type"] == "HEALTH_STATUS_CHANGED"]
    assert len(changed) == 1
    assert changed[0]["data"]["from"] == "UNKNOWN"
    assert changed[0]["data"]["to"] == "CRITICAL"


async def test_recalculate_health_records_no_event_when_unchanged(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, _policy_repo = app_context
    # No policies exist -> health stays UNKNOWN -> UNKNOWN, no transition.
    server = await repo.upsert(_make_server("srv-recalc-2", health=HealthSeverity.UNKNOWN))

    await client.post(f"/api/v1/servers/{server.id}/health/recalculate")

    events = (await client.get(f"/api/v1/servers/{server.id}/events")).json()["items"]
    assert not any(e["event_type"] == "HEALTH_STATUS_CHANGED" for e in events)


async def test_list_events_filters_by_event_type(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, policy_repo = app_context
    server = await repo.upsert(
        _make_server("srv-filter-1", fabric_paths_down=2, health=HealthSeverity.UNKNOWN)
    )
    await policy_repo.upsert(_make_policy("critical-fabric-down-2"))
    await client.post(f"/api/v1/servers/{server.id}/health/recalculate")
    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={})

    resp = await client.get("/api/v1/events", params={"event_type": "MAINTENANCE_ENABLED"})

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["event_type"] == "MAINTENANCE_ENABLED"


async def test_list_events_global_feed_includes_all_server_events(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, repo, _policy_repo = app_context
    server = await repo.upsert(_make_server("srv-global-feed"))
    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "x"})

    resp = await client.get("/api/v1/events")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(e["server_id"] == server.id for e in items)


async def test_events_endpoint_is_read_only(
    app_context: tuple[AsyncClient, MongoServerRepository, MongoHealthPolicyRepository],
) -> None:
    client, _repo, _policy_repo = app_context
    resp = await client.post("/api/v1/events", json={})
    assert resp.status_code == 405
