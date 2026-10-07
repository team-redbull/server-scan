"""`GET /metrics` exports the audit-trail gauges: the refresher is wired in `main.py`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.domain.models.audit_event import Actor, ActorType, AuditEvent, EventType
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.indexes import AUDIT_EVENTS_COLLECTION
from app.main import create_app
from app.utils.ids import new_id

pytestmark = pytest.mark.integration

_OLDEST = datetime(1999, 1, 1, tzinfo=UTC)


def _sample(body: str, name: str) -> float:
    line = next(line for line in body.splitlines() if line.startswith(f"{name} "))
    return float(line.split()[1])


async def test_the_scrape_reports_the_oldest_audit_event() -> None:
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo = app.state.mongo
        event = AuditEvent(
            id=new_id("event"),
            event_type=EventType.SERVER_DELETED,
            actor=Actor(type=ActorType.SYSTEM, id="test"),
            created_at=_OLDEST,
        )
        await MongoAuditEventRepository(mongo).record(event)
        try:
            body = (await client.get("/metrics")).text
        finally:
            await mongo.db[AUDIT_EVENTS_COLLECTION].delete_one({"_id": event.id})

    assert _sample(body, "server_scan_audit_oldest_event_timestamp_seconds") == pytest.approx(
        _OLDEST.timestamp()
    )
    assert _sample(body, "server_scan_audit_events") >= 1
