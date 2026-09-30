"""`tools.prune_servers.prune`: guards, dry-run, backfill, audit (ADR-0037 decision 6)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from tools.prune_servers import PruneResult, prune

from app.application.services.audit_service import AuditService
from app.domain.enums import ManagerType
from app.domain.models.audit_event import EventType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager, ManagerRun
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.indexes import AUDIT_EVENTS_COLLECTION, SERVERS_COLLECTION
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_PROVIDER = "ONEVIEW"


def _doc(index: int, *, listed_at: datetime | None, provider: str = _PROVIDER) -> dict[str, object]:
    return {
        "_id": f"srv_{index}",
        "name": f"node-{index}",
        "name_normalized": f"node-{index}",
        "identity": {"vendor": "hp", "serial": f"SN{index}", "serial_normalized": f"sn{index}"},
        "source_provider": provider,
        "manager_id": "mgr_ov",
        "listed_at": listed_at.isoformat() if listed_at else None,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }


async def _manager(
    mongo: MongoClientHolder, *, partial: bool = False, finished_ago: timedelta = timedelta(0)
) -> None:
    repo = MongoManagerRepository(mongo)
    audit = AuditFields.new()
    await repo.upsert(Manager(_id="mgr_ov", name="ov", type=ManagerType.ONEVIEW, audit=audit))
    finished = utcnow() - finished_ago
    await repo.record_run(
        "mgr_ov",
        ManagerRun(
            started_at=finished,
            finished_at=finished,
            duration_seconds=1,
            servers_fetched=1,
            servers_created=0,
            servers_updated=1,
            ingest_errors=0,
            collection_errors=0,
            partial=partial,
        ),
    )


async def _run(mongo: MongoClientHolder, *, apply: bool, max_fraction: float = 0.5) -> PruneResult:
    return await prune(
        repo=MongoServerRepository(mongo, cursor_secret="s"),
        manager_repo=MongoManagerRepository(mongo),
        audit=AuditService(repo=MongoAuditEventRepository(mongo)),
        cache=None,
        apply=apply,
        now=utcnow(),
        older_than_seconds=86400,
        max_fraction=max_fraction,
        max_run_age_seconds=21600,
    )


async def _seed(mongo: MongoClientHolder, *docs: dict[str, object]) -> None:
    await mongo.db[SERVERS_COLLECTION].insert_many(list(docs))


def _old() -> datetime:
    return utcnow() - timedelta(days=2)


async def test_dry_run_deletes_nothing(mongo_holder: MongoClientHolder) -> None:
    await _manager(mongo_holder)
    await _seed(mongo_holder, _doc(1, listed_at=_old()), _doc(2, listed_at=utcnow()))

    result = await _run(mongo_holder, apply=False)

    assert result.would_delete == ["srv_1"]
    assert await mongo_holder.db[SERVERS_COLLECTION].count_documents({}) == 2


async def test_apply_deletes_and_audits_the_stale_server(mongo_holder: MongoClientHolder) -> None:
    await _manager(mongo_holder)
    await _seed(mongo_holder, _doc(1, listed_at=_old()), _doc(2, listed_at=utcnow()))

    result = await _run(mongo_holder, apply=True)

    assert result.deleted == ["srv_1"]
    assert await mongo_holder.db[SERVERS_COLLECTION].count_documents({"_id": "srv_1"}) == 0
    event = await mongo_holder.db[AUDIT_EVENTS_COLLECTION].find_one({})
    assert event is not None
    assert event["event_type"] == EventType.SERVER_PRUNED.value
    assert event["server_id"] == "srv_1"
    assert event["data"]["source_provider"] == _PROVIDER
    assert event["data"]["serial"] == "SN1"
    assert event["actor"]["type"] == "SYSTEM"


async def test_documents_without_listed_at_are_backfilled_not_deleted(
    mongo_holder: MongoClientHolder,
) -> None:
    await _manager(mongo_holder)
    await _seed(mongo_holder, _doc(1, listed_at=None), _doc(2, listed_at=utcnow()))

    result = await _run(mongo_holder, apply=True)

    assert result.deleted == []
    assert result.backfilled == 1
    stored = await mongo_holder.db[SERVERS_COLLECTION].find_one({"_id": "srv_1"})
    assert stored is not None
    assert isinstance(stored["listed_at"], str)


async def test_a_partial_or_old_run_blocks_pruning(mongo_holder: MongoClientHolder) -> None:
    await _seed(mongo_holder, _doc(1, listed_at=_old()), _doc(2, listed_at=utcnow()))
    await _manager(mongo_holder, partial=True)
    assert _PROVIDER in (await _run(mongo_holder, apply=True)).skipped

    await _manager(mongo_holder, finished_ago=timedelta(hours=7))
    assert _PROVIDER in (await _run(mongo_holder, apply=True)).skipped
    assert await mongo_holder.db[SERVERS_COLLECTION].count_documents({}) == 2


async def test_no_recorded_manager_blocks_pruning(mongo_holder: MongoClientHolder) -> None:
    await _seed(mongo_holder, _doc(1, listed_at=_old()), _doc(2, listed_at=utcnow()))

    assert _PROVIDER in (await _run(mongo_holder, apply=True)).skipped


async def test_the_max_fraction_refuses_a_mass_deletion(mongo_holder: MongoClientHolder) -> None:
    await _manager(mongo_holder)
    await _seed(mongo_holder, _doc(1, listed_at=_old()), _doc(2, listed_at=_old()))

    result = await _run(mongo_holder, apply=True, max_fraction=0.2)

    assert _PROVIDER in result.skipped
    assert await mongo_holder.db[SERVERS_COLLECTION].count_documents({}) == 2
