"""Seeding epoch 0, 1, 2 through the real ingest and real default health policies
produces HEALTH_CHANGED events in both directions."""

from __future__ import annotations

import pytest

from app.application.services.audit_service import AuditService
from app.application.services.bootstrap import ensure_default_health_policies
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService
from app.domain.services.health.metrics import build_default_registry
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import list_managers, list_sites
from app.infrastructure.providers.fake.provider import fake_providers

pytestmark = pytest.mark.integration

SITES = site_catalog("")


async def _seed(mongo: MongoClientHolder, epoch: int) -> None:
    policy_repo = MongoHealthPolicyRepository(mongo)
    registry = build_default_registry()
    await ensure_default_health_policies(policy_repo, registry=registry)
    service = IngestService(
        sites=SITES,
        server_repo=MongoServerRepository(mongo, cursor_secret="test-cursor-secret"),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
        health_service=HealthPolicyService(policy_repo=policy_repo, registry=registry),
        audit=AuditService(repo=MongoAuditEventRepository(mongo)),
    )
    for provider in fake_providers(seed=42, count=400, sites=SITES, epoch=epoch):
        await service.ingest(provider, sites=list_sites(SITES), managers=list_managers())


async def _transitions(mongo: MongoClientHolder) -> set[tuple[str, str]]:
    events = mongo.db["audit_events"].find({"event_type": "HEALTH_CHANGED"})
    return {(e["data"]["from"], e["data"]["to"]) async for e in events}


async def test_epochs_emit_health_changes_both_ways(mongo_holder: MongoClientHolder) -> None:
    await _seed(mongo_holder, 0)
    assert await _transitions(mongo_holder) == set()

    await _seed(mongo_holder, 1)
    first = await _transitions(mongo_holder)
    assert first
    assert any(to == "CRITICAL" for _, to in first)

    await mongo_holder.db["audit_events"].delete_many({})
    await _seed(mongo_holder, 2)
    second = await _transitions(mongo_holder)
    assert any(frm == "CRITICAL" for frm, _ in second)
    assert any(to == "CRITICAL" for _, to in second)
    assert await mongo_holder.db["servers"].count_documents({}) == 400
