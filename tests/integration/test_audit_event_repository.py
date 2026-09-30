"""Integration tests for `MongoAuditEventRepository` against the live dev Mongo."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.domain.models.audit_event import Actor, ActorType, AuditEvent, EventType
from app.errors import CursorInvalidError
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.indexes import AUDIT_EVENTS_COLLECTION
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration


def _event(
    *,
    server_id: str | None = None,
    event_type: EventType,
    actor_id: str = "tester",
    server_name: str | None = None,
    created_at: datetime | None = None,
) -> AuditEvent:
    return AuditEvent(
        id=new_id("event"),
        event_type=event_type,
        server_id=server_id,
        server_name=server_name,
        actor=Actor(type=ActorType.USER, id=actor_id),
        created_at=created_at or utcnow(),
    )


async def test_record_and_read_back(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    event = _event(server_id="srv_x", event_type=EventType.MAINTENANCE_ENABLED)
    await repo.record(event)

    page = await repo.list_page(server_id="srv_x")
    assert len(page.items) == 1
    assert page.items[0].id == event.id
    assert page.items[0].event_type == EventType.MAINTENANCE_ENABLED


async def test_events_are_never_updatable_or_deletable_via_this_repository() -> None:
    """Structural, not behavioral: `MongoAuditEventRepository` exposes no
    `update`/`delete` at all, so adding one fails here before code review.
    """
    public_methods = {name for name in dir(MongoAuditEventRepository) if not name.startswith("_")}
    assert public_methods == {
        "record",
        "list_page",
        "list_actors",
        "count_by_provider_since",
        "rename_legacy_event_types",
    }


async def test_list_page_filters_by_event_type(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await repo.record(_event(event_type=EventType.SERVER_CREATED))
    await repo.record(_event(event_type=EventType.HEALTH_CHANGED))

    page = await repo.list_page(event_type=EventType.HEALTH_CHANGED.value)
    assert len(page.items) == 1
    assert page.items[0].event_type == EventType.HEALTH_CHANGED


async def test_list_page_filters_by_actor_id(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await repo.record(_event(event_type=EventType.SERVER_CREATED, actor_id="alice"))
    await repo.record(_event(event_type=EventType.SERVER_CREATED, actor_id="bob"))

    page = await repo.list_page(actor_id="alice")
    assert len(page.items) == 1
    assert page.items[0].actor.id == "alice"


async def test_pagination_covers_every_event_exactly_once(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    for _ in range(25):
        await repo.record(_event(event_type=EventType.SERVER_CREATED))

    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(20):
        page = await repo.list_page(cursor=cursor, page_size=7)
        for item in page.items:
            assert item.id not in seen
            seen.add(item.id)
        if not page.has_more:
            break
        cursor = page.next_cursor
    else:
        pytest.fail("did not terminate within the expected number of pages")

    assert len(seen) == 25


async def test_malformed_cursor_raises_cursor_invalid(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    with pytest.raises(CursorInvalidError):
        await repo.list_page(cursor="not-a-valid-cursor!!!")


async def test_global_feed_query_uses_index_not_collection_scan(
    mongo_holder: MongoClientHolder,
) -> None:
    collection = mongo_holder.db[AUDIT_EVENTS_COLLECTION]
    explain = await collection.find({}).sort([("created_at", -1), ("_id", -1)]).explain()
    explain_str = json.dumps(explain)
    assert "COLLSCAN" not in explain_str
    assert "IXSCAN" in explain_str


async def test_server_scoped_query_uses_index_not_collection_scan(
    mongo_holder: MongoClientHolder,
) -> None:
    collection = mongo_holder.db[AUDIT_EVENTS_COLLECTION]
    explain = await (
        collection.find({"server_id": "srv_x"}).sort([("created_at", -1), ("_id", -1)]).explain()
    )
    explain_str = json.dumps(explain)
    assert "COLLSCAN" not in explain_str
    assert "IXSCAN" in explain_str


async def test_count_by_provider_since_groups_by_data_provider(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    for provider in ("ONEVIEW", "ONEVIEW", None):
        event = _event(event_type=EventType.SERVER_DELETED)
        event.data = {"source_provider": provider} if provider else {}
        await repo.record(event)

    counts = await repo.count_by_provider_since(
        EventType.SERVER_DELETED.value, datetime(2000, 1, 1, tzinfo=UTC)
    )
    assert counts == {"ONEVIEW": 2, "unknown": 1}


async def test_since_is_inclusive_and_until_is_exclusive(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    for day in (1, 2, 3):
        await repo.record(
            _event(
                event_type=EventType.SERVER_CREATED, created_at=datetime(2026, 1, day, tzinfo=UTC)
            )
        )

    page = await repo.list_page(
        since=datetime(2026, 1, 2, tzinfo=UTC), until=datetime(2026, 1, 3, tzinfo=UTC)
    )
    assert [e.created_at.day for e in page.items] == [2]
    assert len((await repo.list_page(since=datetime(2026, 1, 2))).items) == 2  # naive = UTC


async def test_server_name_matches_current_servers_and_snapshots(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await mongo_holder.db["servers"].insert_one(
        {"_id": "srv_a", "name": "OCP-Tomer-01", "name_normalized": "ocp-tomer-01"}
    )
    await repo.record(_event(server_id="srv_a", event_type=EventType.SERVER_UPDATED))
    await repo.record(
        _event(server_id="srv_gone", server_name="ocp-tomer-02", event_type=EventType.SERVER_PRUNED)
    )
    await repo.record(
        _event(server_id="srv_b", server_name="other-box", event_type=EventType.SERVER_CREATED)
    )

    page = await repo.list_page(server_name="TOMER")
    assert sorted(e.server_id or "" for e in page.items) == ["srv_a", "srv_gone"]
    both = await repo.list_page(server_name="tomer", event_type="SERVER_PRUNED", page_size=1)
    assert [e.server_id for e in both.items] == ["srv_gone"]
    assert (await repo.list_page(server_name="nomatch")).items == []


async def test_server_name_filter_combines_with_the_cursor(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    for _ in range(5):
        await repo.record(
            _event(server_id="s", server_name="box-x", event_type=EventType.SERVER_UPDATED)
        )
    first = await repo.list_page(server_name="box", page_size=3)
    second = await repo.list_page(server_name="box", page_size=3, cursor=first.next_cursor)
    assert len(first.items) == 3
    assert len(second.items) == 2


async def test_legacy_events_load_and_get_their_name_in_one_lookup(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await mongo_holder.db["servers"].insert_many(
        [
            {"_id": "srv_1", "name": "live-1", "name_normalized": "live-1"},
            {"_id": "srv_2", "name": "live-2", "name_normalized": "live-2"},
        ]
    )
    for i, sid in enumerate(("srv_1", "srv_2", "srv_dead", None)):
        await mongo_holder.db[AUDIT_EVENTS_COLLECTION].insert_one(
            {
                "_id": f"legacy{i}",
                "event_type": "SERVER_UPDATED",
                "server_id": sid,
                "actor": {"type": "USER", "id": "old"},
                "created_at": f"2025-01-01T00:00:0{i}Z",
                "data": {},
            }
        )

    names = {e.server_id: e.server_name for e in (await repo.list_page()).items}
    assert names == {"srv_1": "live-1", "srv_2": "live-2", "srv_dead": None, None: None}


async def test_list_actors_counts_and_takes_latest_identity(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    old = _event(event_type=EventType.SERVER_CREATED, actor_id="alice")
    old.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    new = _event(event_type=EventType.SERVER_CREATED, actor_id="alice")
    new.actor.display = "Alice A"
    await repo.record(old)
    await repo.record(new)
    await repo.record(_event(event_type=EventType.SERVER_CREATED, actor_id="bob"))

    actors = await repo.list_actors()
    assert [(a.id, a.event_count, a.display) for a in actors] == [
        ("alice", 2, "Alice A"),
        ("bob", 1, None),
    ]


async def _insert_legacy(mongo_holder: MongoClientHolder) -> str:
    doc = _event(event_type=EventType.HEALTH_CHANGED).model_dump(by_alias=True, mode="json")
    doc["event_type"] = "HEALTH_STATUS_CHANGED"
    await mongo_holder.db[AUDIT_EVENTS_COLLECTION].insert_one(doc)
    return doc["_id"]


async def test_legacy_event_type_loads_as_the_new_name(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await _insert_legacy(mongo_holder)

    page = await repo.list_page()
    assert [e.event_type for e in page.items] == [EventType.HEALTH_CHANGED]


async def test_event_type_filter_matches_legacy_rows_by_either_name(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await _insert_legacy(mongo_holder)
    await repo.record(_event(event_type=EventType.HEALTH_CHANGED))
    await repo.record(_event(event_type=EventType.SERVER_CREATED))

    for name in ("HEALTH_CHANGED", "HEALTH_STATUS_CHANGED"):
        page = await repo.list_page(event_type=name)
        assert len(page.items) == 2
        assert {e.event_type for e in page.items} == {EventType.HEALTH_CHANGED}


async def test_rename_legacy_event_types_is_idempotent(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await _insert_legacy(mongo_holder)
    await _insert_legacy(mongo_holder)

    assert await repo.rename_legacy_event_types() == 2
    assert await repo.rename_legacy_event_types() == 0
    stored = mongo_holder.db[AUDIT_EVENTS_COLLECTION]
    assert await stored.count_documents({"event_type": "HEALTH_STATUS_CHANGED"}) == 0
    assert await stored.count_documents({"event_type": "HEALTH_CHANGED"}) == 2
