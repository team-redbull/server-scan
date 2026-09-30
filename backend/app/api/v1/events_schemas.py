"""`GET /api/v1/events`, `GET /api/v1/servers/{server_id}/events` schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.domain.models.audit_event import Actor, EventType


class AuditEventResponse(BaseModel):
    """The public representation of one audit event."""

    id: str
    event_type: EventType
    server_id: str | None
    server_name: str | None
    actor: Actor
    request_id: str | None
    created_at: datetime
    data: dict[str, Any]


class EventPageInfo(BaseModel):
    """Keyset paging metadata for a page of audit events."""

    next_cursor: str | None
    has_more: bool
    page_size: int


class AuditEventListResponse(BaseModel):
    """One page of audit events plus its paging metadata."""

    items: list[AuditEventResponse]
    page: EventPageInfo


class EventActor(BaseModel):
    """One distinct actor seen in the audit log."""

    id: str
    type: str
    display: str | None
    event_count: int


class EventActorListResponse(BaseModel):
    """Distinct actors, busiest first."""

    items: list[EventActor]
