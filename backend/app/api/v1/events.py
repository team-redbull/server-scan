"""`GET /api/v1/events`, `GET /api/v1/servers/{server_id}/events`.

Read-only by design (only the retention job in `tools/` deletes, ADR-0045): there
is deliberately no `POST /events` — the only way an event is created is a side
effect of a real mutation
(`app.application.services.audit_service.AuditService.record`, called
from the services that own each mutation), never a direct API write. That
is what makes "the audit log reflects what actually happened" true instead
of "the audit log reflects what someone claimed happened."
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.events_schemas import (
    AuditEventListResponse,
    AuditEventResponse,
    EventActor,
    EventActorListResponse,
    EventPageInfo,
)
from app.dependencies import get_mongo_holder
from app.infrastructure.mongodb.audit_event_repository import (
    AuditEventPage,
    MongoAuditEventRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder

router = APIRouter(prefix="/api/v1", tags=["events"])

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


async def _event_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoAuditEventRepository:
    """
    Build the audit event repository for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.

    Returns:
        MongoAuditEventRepository: A repository bound to that client.
    """
    return MongoAuditEventRepository(mongo)


def _to_response(page: AuditEventPage, *, page_size: int) -> AuditEventListResponse:
    return AuditEventListResponse(
        items=[AuditEventResponse.model_validate(e.model_dump()) for e in page.items],
        page=EventPageInfo(
            next_cursor=page.next_cursor, has_more=page.has_more, page_size=page_size
        ),
    )


@router.get("/events", response_model=AuditEventListResponse)
async def list_events(
    repo: Annotated[MongoAuditEventRepository, Depends(_event_repo)],
    server_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    server_name: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
) -> AuditEventListResponse:
    """
    List audit events across every server, newest first, with keyset paging.

    Args:
        repo (MongoAuditEventRepository): The audit event repository.
        server_id (str | None): Restrict to events for this server.
        event_type (str | None): Restrict to this event type.
        actor_id (str | None): Restrict to events recorded by this actor (exact `actor.id`).
        since (datetime | None): Only events at or after this instant (inclusive).
        until (datetime | None): Only events before this instant (exclusive).
        server_name (str | None): Case-insensitive substring of the server's name;
            matches current servers (first 5000 ids) and the name snapshotted on the
            event, so pruned servers still match.
        cursor (str | None): Opaque cursor from a previous page's response.
        page_size (int): Maximum items to return, 1-200.

    Returns:
        AuditEventListResponse: The matching page of events; filters combine with AND.
    """
    page = await repo.list_page(
        server_id=server_id,
        event_type=event_type,
        actor_id=actor_id,
        since=since,
        until=until,
        server_name=server_name,
        cursor=cursor,
        page_size=page_size,
    )
    return _to_response(page, page_size=page_size)


@router.get("/events/actors", response_model=EventActorListResponse)
async def list_event_actors(
    repo: Annotated[MongoAuditEventRepository, Depends(_event_repo)],
) -> EventActorListResponse:
    """
    List distinct event actors by `actor.id`, busiest first (at most 200).

    Args:
        repo (MongoAuditEventRepository): The audit event repository.

    Returns:
        EventActorListResponse: Each actor's type/display from its latest event, and its count.
    """
    actors = await repo.list_actors()
    return EventActorListResponse(
        items=[
            EventActor(id=a.id, type=a.type, display=a.display, event_count=a.event_count)
            for a in actors
        ]
    )


@router.get("/servers/{server_id}/events", response_model=AuditEventListResponse)
async def list_server_events(
    server_id: str,
    repo: Annotated[MongoAuditEventRepository, Depends(_event_repo)],
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
) -> AuditEventListResponse:
    """
    List audit events for one server, newest first, with keyset paging.

    Args:
        server_id (str): The server's ID.
        repo (MongoAuditEventRepository): The audit event repository.
        cursor (str | None): Opaque cursor from a previous page's response.
        page_size (int): Maximum items to return, 1-200.

    Returns:
        AuditEventListResponse: The matching page of events.
    """
    page = await repo.list_page(server_id=server_id, cursor=cursor, page_size=page_size)
    return _to_response(page, page_size=page_size)
