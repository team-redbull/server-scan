"""`tools.prune_events.prune_events`: report-only, batching, cutoff, one audit event (ADR-0045)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tools.prune_events import RETENTION_ACTOR, RetentionOutcome, prune_events

from app.application.services.audit_service import AuditService
from app.domain.models.audit_event import EventType
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository, _iso
from app.infrastructure.mongodb.indexes import AUDIT_EVENTS_COLLECTION
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_DAYS = 180


def _doc(
    index: int, created_at: datetime, event_type: EventType = EventType.HEALTH_CHANGED
) -> dict[str, Any]:
    return {
        "_id": f"evt_{index}",
        "event_type": event_type.value,
        "server_id": f"srv_{index}",
        "actor": {"type": "SYSTEM", "id": "ingestion"},
        "created_at": _iso(created_at),
        "data": {},
    }


async def _seed(mongo: MongoClientHolder, *docs: dict[str, Any]) -> None:
    await mongo.db[AUDIT_EVENTS_COLLECTION].insert_many(list(docs))


async def _run(
    mongo: MongoClientHolder,
    *,
    report_only: bool,
    now: datetime | None = None,
    batch_size: int = 100,
    max_batches: int = 200,
) -> RetentionOutcome:
    repo = MongoAuditEventRepository(mongo)
    return await prune_events(
        repo=repo,
        audit=AuditService(repo=repo),
        retention_days=_DAYS,
        report_only=report_only,
        batch_size=batch_size,
        max_batches=max_batches,
        now=now or utcnow(),
    )


async def _ids(mongo: MongoClientHolder) -> set[str]:
    docs = await mongo.db[AUDIT_EVENTS_COLLECTION].find({}, {"_id": 1}).to_list(None)
    return {d["_id"] for d in docs}


async def _purged_events(mongo: MongoClientHolder) -> list[dict[str, Any]]:
    return (
        await mongo.db[AUDIT_EVENTS_COLLECTION].find({"event_type": "AUDIT_PURGED"}).to_list(None)
    )


def _old(days: int = _DAYS + 10) -> datetime:
    return utcnow() - timedelta(days=days)


async def test_report_only_deletes_nothing_and_records_no_event(
    mongo_holder: MongoClientHolder,
) -> None:
    await _seed(mongo_holder, _doc(1, _old()), _doc(2, utcnow()))

    outcome = await _run(mongo_holder, report_only=True)

    assert outcome.report_only is True
    assert outcome.would_delete == 1
    assert await _ids(mongo_holder) == {"evt_1", "evt_2"}
    assert await _purged_events(mongo_holder) == []


async def test_apply_deletes_only_older_events_including_server_pruned(
    mongo_holder: MongoClientHolder,
) -> None:
    await _seed(
        mongo_holder,
        _doc(1, _old()),
        _doc(2, _old(400), EventType.SERVER_PRUNED),
        _doc(3, utcnow()),
        _doc(4, utcnow() - timedelta(days=_DAYS - 5), EventType.SERVER_PRUNED),
    )

    await _run(mongo_holder, report_only=False)

    remaining = await _ids(mongo_holder)
    assert {"evt_3", "evt_4"} <= remaining
    assert "evt_1" not in remaining
    assert "evt_2" not in remaining


async def test_exactly_one_audit_purged_event_with_the_run_data(
    mongo_holder: MongoClientHolder,
) -> None:
    oldest, newest = _old(300), _old(200)
    await _seed(mongo_holder, _doc(1, oldest), _doc(2, newest), _doc(3, utcnow()))

    await _run(mongo_holder, report_only=False)

    events = await _purged_events(mongo_holder)
    assert len(events) == 1
    event = events[0]
    assert event["actor"]["type"] == "SYSTEM"
    assert event["actor"]["id"] == RETENTION_ACTOR.id
    assert event["server_id"] is None
    data = event["data"]
    assert data["deleted"] == 2
    assert data["retention_days"] == _DAYS
    assert data["batches"] == 1
    assert data["truncated"] is False
    assert data["reason"] == "age-based retention"
    assert data["oldest_deleted_created_at"] == _iso(oldest)
    assert data["newest_deleted_created_at"] == _iso(newest)


async def test_batching_spans_several_batches(mongo_holder: MongoClientHolder) -> None:
    await _seed(mongo_holder, *[_doc(i, _old(200 + i)) for i in range(250)], _doc(999, utcnow()))

    outcome = await _run(mongo_holder, report_only=False, batch_size=100)

    assert outcome.deleted == 250
    assert outcome.batches == 3
    assert outcome.truncated is False
    assert "evt_999" in await _ids(mongo_holder)


async def test_max_batches_truncates_and_a_rerun_finishes(mongo_holder: MongoClientHolder) -> None:
    await _seed(mongo_holder, *[_doc(i, _old(200 + i)) for i in range(250)])

    first = await _run(mongo_holder, report_only=False, batch_size=100, max_batches=2)

    assert first.deleted == 200
    assert first.truncated is True
    assert (await _purged_events(mongo_holder))[0]["data"]["truncated"] is True

    second = await _run(mongo_holder, report_only=False, batch_size=100, max_batches=2)

    assert second.deleted == 50
    assert second.truncated is False


async def test_an_event_exactly_at_the_cutoff_is_kept(mongo_holder: MongoClientHolder) -> None:
    now = utcnow()
    cutoff = now - timedelta(days=_DAYS)
    await _seed(mongo_holder, _doc(1, cutoff), _doc(2, cutoff - timedelta(milliseconds=1)))

    await _run(mongo_holder, report_only=False, now=now)

    remaining = await _ids(mongo_holder)
    assert "evt_1" in remaining
    assert "evt_2" not in remaining


async def test_a_rerun_is_idempotent_and_records_no_second_event(
    mongo_holder: MongoClientHolder,
) -> None:
    await _seed(mongo_holder, _doc(1, _old()), _doc(2, utcnow()))

    await _run(mongo_holder, report_only=False)
    second = await _run(mongo_holder, report_only=False)

    assert second.deleted == 0
    assert len(await _purged_events(mongo_holder)) == 1


async def test_stored_z_strings_compare_against_the_rendered_cutoff(
    mongo_holder: MongoClientHolder,
) -> None:
    stored = "2020-01-01T00:00:00Z"
    doc = _doc(1, utcnow())
    doc["created_at"] = stored
    await _seed(mongo_holder, doc)

    preview = await MongoAuditEventRepository(mongo_holder).preview_before(
        utcnow() - timedelta(days=_DAYS)
    )

    assert preview.count == 1
    assert preview.oldest == stored


async def test_purge_refuses_a_cutoff_inside_the_last_seven_days(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoAuditEventRepository(mongo_holder)

    with pytest.raises(ValueError, match="7 days"):
        await repo.purge_before(utcnow() - timedelta(days=1), batch_size=100, max_batches=1)


class _FlakyRepo(MongoAuditEventRepository):
    """Its second `delete_many` raises, as a Mongo failover or a killed pod would."""

    deletes = 0

    @property
    def _collection(self) -> Any:
        real = super()._collection
        repo = self

        class _Wrapper:
            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

            async def delete_many(self, *args: Any, **kwargs: Any) -> Any:
                repo.deletes += 1
                if repo.deletes == 2:
                    raise RuntimeError("mongo went away")
                return await real.delete_many(*args, **kwargs)

        return _Wrapper()


async def test_a_purge_that_fails_partway_still_leaves_a_record(
    mongo_holder: MongoClientHolder,
) -> None:
    await _seed(mongo_holder, *[_doc(i, _old(200 + i)) for i in range(250)])
    repo = _FlakyRepo(mongo_holder)

    with pytest.raises(RuntimeError, match="went away"):
        await prune_events(
            repo=repo,
            audit=AuditService(repo=repo),
            retention_days=_DAYS,
            report_only=False,
            batch_size=100,
            max_batches=200,
            now=utcnow(),
        )

    (purged,) = await _purged_events(mongo_holder)
    assert purged["data"]["deleted"] == 100
    assert purged["data"]["complete"] is False
    assert len(await _ids(mongo_holder)) == 150 + 1


async def test_the_seven_day_floor_is_exact(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)

    with pytest.raises(ValueError, match="7 days"):
        await repo.purge_before(utcnow() - timedelta(days=6), batch_size=100, max_batches=1)
    result = await repo.purge_before(utcnow() - timedelta(days=8), batch_size=100, max_batches=1)
    assert result.deleted == 0


async def test_a_whole_second_timestamp_without_microseconds_is_purged(
    mongo_holder: MongoClientHolder,
) -> None:
    whole_second = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
    await _seed(mongo_holder, _doc(1, whole_second), _doc(2, utcnow()))

    outcome = await _run(mongo_holder, report_only=False)

    assert outcome.deleted == 1
    assert "evt_1" not in await _ids(mongo_holder)
