"""
The one path through which an audit event is ever recorded.

No other code in this codebase constructs an `AuditEvent` or calls
`MongoAuditEventRepository.record()` directly — every mutation that needs
an audit trail (classification rule CRUD, health policy CRUD,
classification/health changes, maintenance changes) goes through
`AuditService.record()`, so the id-generation, timestamping, and "which
repository method actually persists this" decisions live in exactly one
place.
"""

from __future__ import annotations

from typing import Any

from app.domain.models.audit_event import Actor, ActorType, AuditEvent, EventType
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


class AuditService:
    """Records audit events through the one repository that persists them."""

    def __init__(self, *, repo: MongoAuditEventRepository) -> None:
        """
        Initialize the service with its audit event repository.

        Args:
            repo (MongoAuditEventRepository): The repository events are recorded to.
        """
        self._repo = repo

    async def record(
        self,
        event_type: EventType,
        *,
        actor: Actor,
        server_id: str | None = None,
        request_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """
        Build and persist an audit event.

        Args:
            event_type (EventType): What kind of event occurred.
            actor (Actor): Who or what caused it.
            server_id (str | None): The server it concerns, if any.
            request_id (str | None): The originating API request id, if any.
            data (dict[str, Any] | None): Event-specific payload; defaults to `{}`.

        Returns:
            AuditEvent: The persisted event, as returned by the repository.
        """
        event = AuditEvent(
            id=new_id("event"),
            event_type=event_type,
            server_id=server_id,
            actor=actor,
            request_id=request_id,
            created_at=utcnow(),
            data=data or {},
        )
        return await self._repo.record(event)


# A fixed id, so the pipeline's history is one `actor.id`-filtered query.
SYSTEM_INGEST_ACTOR = Actor(type=ActorType.SYSTEM, id="ingestion")
