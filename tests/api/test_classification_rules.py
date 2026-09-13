"""
API tests for `/api/v1/classification-rules` against a real app and the live
dev Mongo. Read-only endpoints only, and that is the contract under test:
rules ship with the platform, and the last test guards that the write verbs
stay removed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.domain.enums import InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.classification_rule import ClassificationRule, RuleScope
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

_CURSOR_SECRET = "test-cursor-secret"


def _make_server(
    name: str, *, vendor: Vendor = Vendor.DELL, index: int = 0, site_id: str | None = None
) -> Server:
    now = utcnow()
    serial = f"CRAPITEST{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"crapi-test-uuid-{index:06d}",
        ),
        site_id=site_id,
        classification=Classification(installation_type=InstallationType.UNCLASSIFIED),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


def _make_rule(
    name: str,
    *,
    priority: int = 200,
    source: str = "GLOBAL_CUSTOM",
    scope: RuleScope | None = None,
    system: bool = False,
    enabled: bool = True,
    pattern: str = r"^ocp-.*",
) -> ClassificationRule:
    now = utcnow()
    return ClassificationRule(
        _id=new_id("classification_rule"),
        name=name,
        installation_type=InstallationType.HOSTED_CLUSTER,
        scope=scope or RuleScope(),
        field="name",
        pattern=pattern,
        source=source,
        priority=priority,
        system=system,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def app_context() -> AsyncIterator[
    tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository]
]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        for name in ("servers", "sites", "managers", "classification_rules"):
            await mongo.db[name].delete_many({})

        rule_repo = MongoClassificationRuleRepository(mongo)
        server_repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        yield client, rule_repo, server_repo

        for name in ("servers", "sites", "managers", "classification_rules"):
            await mongo.db[name].delete_many({})


async def test_list_returns_every_rule(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _ = app_context
    await rule_repo.upsert(_make_rule("alpha"))
    await rule_repo.upsert(_make_rule("beta", enabled=False))

    resp = await client.get("/api/v1/classification-rules")

    assert resp.status_code == 200
    assert {item["name"] for item in resp.json()["items"]} == {"alpha", "beta"}


async def test_list_exposes_the_pattern_and_the_field_it_matches(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    """Without these a listed rule is a name and a verdict with nothing
    connecting them, which tells an operator nothing about why a server
    was classified the way it was.
    """
    client, rule_repo, _ = app_context
    await rule_repo.upsert(_make_rule("alpha", pattern=r"^ocp4-hypershift-"))

    item = (await client.get("/api/v1/classification-rules")).json()["items"][0]

    assert item["field"] == "name"
    assert item["pattern"] == r"^ocp4-hypershift-"
    assert item["flags"]["ignore_case"] is True


async def test_list_filters_by_enabled(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _ = app_context
    await rule_repo.upsert(_make_rule("on"))
    await rule_repo.upsert(_make_rule("off", enabled=False))

    enabled = (await client.get("/api/v1/classification-rules?enabled=true")).json()
    disabled = (await client.get("/api/v1/classification-rules?enabled=false")).json()

    assert [i["name"] for i in enabled["items"]] == ["on"]
    assert [i["name"] for i in disabled["items"]] == ["off"]


async def test_get_returns_one_rule(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _ = app_context
    rule = await rule_repo.upsert(_make_rule("alpha"))

    resp = await client.get(f"/api/v1/classification-rules/{rule.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "alpha"


async def test_get_404_for_missing(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _, _ = app_context

    assert (await client.get("/api/v1/classification-rules/nope")).status_code == 404


async def test_no_endpoint_can_change_a_rule(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    """Rules ship with the platform, so no write verb may be reachable.
    405, not 404: the paths still exist for `GET`; a 404 would mean the
    read endpoints had gone too.
    """
    client, rule_repo, _ = app_context
    rule = await rule_repo.upsert(_make_rule("alpha"))
    body = {"name": "new", "installation_type": "UPI", "field": "name", "pattern": "^x"}

    responses = {
        "create": await client.post("/api/v1/classification-rules", json=body),
        "update": await client.patch(
            f"/api/v1/classification-rules/{rule.id}", json={"enabled": False}
        ),
        "delete": await client.delete(f"/api/v1/classification-rules/{rule.id}"),
        "preview": await client.post("/api/v1/classification-rules/preview", json=body),
    }

    for action, resp in responses.items():
        assert resp.status_code == 405, f"{action} answered {resp.status_code}, not 405"

    unchanged = await rule_repo.get_by_id(rule.id)
    assert unchanged is not None
    assert unchanged.enabled is True
