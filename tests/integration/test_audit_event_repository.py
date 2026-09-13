"""Integration tests for `MongoAuditEventRepository` against the live dev Mongo."""

from __future__ import annotations

import json

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
    *, server_id: str | None = None, event_type: EventType, actor_id: str = "tester"
) -> AuditEvent:
    return AuditEvent(
        id=new_id("event"),
        event_type=event_type,
        server_id=server_id,
        actor=Actor(type=ActorType.USER, id=actor_id),
        created_at=utcnow(),
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
    assert public_methods == {"record", "list_page"}


async def test_list_page_filters_by_event_type(mongo_holder: MongoClientHolder) -> None:
    repo = MongoAuditEventRepository(mongo_holder)
    await repo.record(_event(event_type=EventType.SERVER_CREATED))
    await repo.record(_event(event_type=EventType.HEALTH_STATUS_CHANGED))

    page = await repo.list_page(event_type=EventType.HEALTH_STATUS_CHANGED.value)
    assert len(page.items) == 1
    assert page.items[0].event_type == EventType.HEALTH_STATUS_CHANGED


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
