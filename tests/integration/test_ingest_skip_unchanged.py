"""A server whose collected content did not change is not rewritten (ADR-0044).

The guard test re-ingests the whole fake fleet through the real build path with the classification
and health engines on: a new per-run stamp added to `IngestService._build_server` without being
listed as volatile makes every server "changed", and this test fails.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping

import pytest

from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService
from app.domain.enums import ManagerType
from app.domain.models.maintenance import Maintenance
from app.domain.models.server import Server
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import list_managers, list_sites
from app.infrastructure.providers.fake.provider import fake_providers

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"
_SERIAL = "SN-SKIP-1"
_ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)


class _OneShotProvider(ServerInventoryProvider):
    provider_type = ManagerType.UCS_CENTRAL.value

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


class _TouchRacingRepo(MongoServerRepository):
    """Another writer toggles maintenance just before ingest's conditional touch lands."""

    raced = False

    async def touch_seen(
        self, server_id: str, *, expected_revision: int, fields: Mapping[str, object]
    ) -> bool:
        if not self.raced:
            self.raced = True
            current = await self.get_by_id(server_id)
            assert current is not None
            stored_revision = current.revision
            current.maintenance = Maintenance(enabled=True, reason="admin, mid-ingest")
            current.revision = stored_revision + 1
            await self.upsert_with_revision_check(current, expected_revision=stored_revision)
        return await super().touch_seen(
            server_id, expected_revision=expected_revision, fields=fields
        )


def _service(mongo: MongoClientHolder, repo: MongoServerRepository | None = None) -> IngestService:
    return IngestService(
        sites=site_catalog(""),
        server_repo=repo or MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
        classification_service=ClassificationService(
            rule_repo=MongoClassificationRuleRepository(mongo), engine=_ENGINE
        ),
        health_service=HealthPolicyService(
            policy_repo=MongoHealthPolicyRepository(mongo), registry=build_default_registry()
        ),
    )


def _provider_server(**overrides: object) -> ProviderServer:
    base: dict[str, object] = {
        "external_id": "ucsm://domain-1/sys/chassis-1/blade-7",
        "vendor": "cisco",
        "name": "ocp4-prod-tlv-infra-07",
        "serial": _SERIAL,
        "model": "UCSB-B200-M5",
    }
    base.update(overrides)
    return ProviderServer(**base)  # ty: ignore[invalid-argument-type]


async def _the_server(repo: MongoServerRepository) -> Server:
    page = await repo.list_page(
        filters={"identity.serial": _SERIAL},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=5,
        with_count=False,
    )
    (server,) = page.items
    return server


async def _clean(mongo: MongoClientHolder) -> None:
    await mongo.db["servers"].delete_many({"identity.serial": _SERIAL})


async def test_reingesting_the_whole_fake_fleet_changes_nothing(
    mongo_holder: MongoClientHolder,
) -> None:
    service = _service(mongo_holder)
    fetched = created = 0
    for provider in fake_providers(seed=23, count=40):
        summary = await service.ingest(provider, sites=list_sites(), managers=list_managers())
        fetched += summary.fetched
        created += summary.created

    unchanged = updated = errors = 0
    for provider in fake_providers(seed=23, count=40):
        summary = await service.ingest(provider, sites=list_sites(), managers=list_managers())
        unchanged += summary.unchanged
        updated += summary.updated
        errors += summary.errors

    assert errors == 0
    assert created == fetched
    assert updated == 0, "a per-run stamp is missing from ingest._VOLATILE_TOP_LEVEL / _stable_view"
    assert unchanged == fetched


async def test_an_unchanged_server_keeps_its_revision_but_refreshes_its_stamps(
    mongo_holder: MongoClientHolder,
) -> None:
    await _clean(mongo_holder)
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_provider_server()))
    before = await _the_server(repo)

    summary = await service.ingest(_OneShotProvider(_provider_server()))
    after = await _the_server(repo)

    assert (summary.unchanged, summary.updated, summary.created) == (1, 0, 0)
    assert after.revision == before.revision
    assert after.updated_at == before.updated_at
    assert after.classification.classification_version == (
        before.classification.classification_version
    )
    assert after.last_seen_at is not None and before.last_seen_at is not None
    assert after.last_seen_at > before.last_seen_at
    assert after.listed_at is not None and before.listed_at is not None
    assert after.listed_at > before.listed_at
    assert after.health.evaluated_at is not None and before.health.evaluated_at is not None
    assert after.health.evaluated_at > before.health.evaluated_at
    await _clean(mongo_holder)


async def test_live_readings_are_refreshed_on_every_run_without_counting_as_a_change(
    mongo_holder: MongoClientHolder,
) -> None:
    await _clean(mongo_holder)
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    service = _service(mongo_holder)

    def run(watts: float, celsius: float) -> ProviderServer:
        return _provider_server(
            gpus=({"id": "gpu0", "temperature_celsius": celsius, "power_watts": watts},),
            psus=({"id": "PSU1", "power_watts": watts},),
        )

    await service.ingest(_OneShotProvider(run(210.0, 61.0)))
    before = await _the_server(repo)
    summary = await service.ingest(_OneShotProvider(run(305.5, 68.0)))
    after = await _the_server(repo)

    assert (summary.unchanged, summary.updated) == (1, 0)
    assert after.revision == before.revision
    assert after.hardware.gpus[0].temperature_celsius == 68.0
    assert after.hardware.gpus[0].power_watts == 305.5
    assert after.hardware.power.psus[0].power_watts == 305.5
    await _clean(mongo_holder)


async def test_a_content_change_is_a_full_write(mongo_holder: MongoClientHolder) -> None:
    await _clean(mongo_holder)
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_provider_server()))
    before = await _the_server(repo)

    summary = await service.ingest(_OneShotProvider(_provider_server(model="UCSB-B200-M6")))
    after = await _the_server(repo)

    assert (summary.updated, summary.unchanged) == (1, 0)
    assert after.model == "UCSB-B200-M6"
    assert after.revision == before.revision + 1
    assert after.classification.classification_version == (
        before.classification.classification_version + 1
    )
    await _clean(mongo_holder)


async def test_a_write_between_the_read_and_the_touch_is_not_lost(
    mongo_holder: MongoClientHolder,
) -> None:
    await _clean(mongo_holder)
    plain = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    await _service(mongo_holder).ingest(_OneShotProvider(_provider_server()))
    first = await _the_server(plain)

    racing = _TouchRacingRepo(mongo_holder, cursor_secret=_CURSOR_SECRET)
    summary = await _service(mongo_holder, racing).ingest(_OneShotProvider(_provider_server()))
    after = await _the_server(plain)

    assert racing.raced
    assert summary.errors == 0
    assert after.maintenance.enabled is True
    assert after.revision == first.revision + 1
    await _clean(mongo_holder)


async def test_an_unreachable_stub_keeps_last_seen_but_stays_listed(
    mongo_holder: MongoClientHolder,
) -> None:
    await _clean(mongo_holder)
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    service = _service(mongo_holder)
    stub = _provider_server(reachable=False, unreachable_reason="timeout")
    await service.ingest(_OneShotProvider(stub))
    before = await _the_server(repo)

    summary = await service.ingest(_OneShotProvider(stub))
    after = await _the_server(repo)

    assert (summary.unchanged, summary.updated) == (1, 0)
    assert after.last_seen_at == before.last_seen_at
    assert after.listed_at is not None and before.listed_at is not None
    assert after.listed_at > before.listed_at
    await _clean(mongo_holder)


async def test_a_document_written_before_a_field_existed_is_rewritten_not_just_touched(
    mongo_holder: MongoClientHolder,
) -> None:
    await _clean(mongo_holder)
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_provider_server()))
    stored = await _the_server(repo)
    await mongo_holder.db["servers"].update_one(
        {"_id": stored.id}, {"$unset": {"unread_fields": "", "health.evaluated_at": ""}}
    )

    summary = await service.ingest(_OneShotProvider(_provider_server()))

    raw = await mongo_holder.db["servers"].find_one({"_id": stored.id})
    assert raw is not None
    assert (summary.updated, summary.unchanged) == (1, 0)
    assert "unread_fields" in raw
    assert raw["health"]["evaluated_at"] is not None
    await _clean(mongo_holder)
