"""The `audit_events` collection: an immutable, append-only log of every platform mutation.

Append-only is enforced by construction, not by a database permission
alone: `MongoAuditEventRepository` (infrastructure layer) exposes only
`record()`, which only ever calls `insert_one` — there is no `update`/
`delete` method on that repository at all, so no code path in this
codebase can alter or remove a recorded event, not even by accident.

`EventType` is a closed, append-only registry (mirroring `ErrorCode`'s own
convention in `app.errors`): new values are added at the end, existing
values are never renumbered or removed, since stored events reference them
by string and old events must stay readable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ActorType(StrEnum):
    """The kind of principal that performed an audited action."""

    SYSTEM = "SYSTEM"
    USER = "USER"
    TOKEN = "TOKEN"  # noqa: S105 - an actor-type label, not a credential


class Actor(BaseModel):
    """The principal an `AuditEvent` records as having performed it."""

    type: ActorType
    id: str
    display: str | None = None


class EventType(StrEnum):
    """The closed, append-only registry of audit event kinds."""

    SERVER_CREATED = "SERVER_CREATED"
    SERVER_UPDATED = "SERVER_UPDATED"
    SERVER_DELETED = "SERVER_DELETED"
    CLASSIFICATION_CHANGED = "CLASSIFICATION_CHANGED"
    CLASSIFICATION_RULE_CREATED = "CLASSIFICATION_RULE_CREATED"
    CLASSIFICATION_RULE_UPDATED = "CLASSIFICATION_RULE_UPDATED"
    CLASSIFICATION_RULE_DELETED = "CLASSIFICATION_RULE_DELETED"
    HEALTH_POLICY_CREATED = "HEALTH_POLICY_CREATED"
    HEALTH_POLICY_UPDATED = "HEALTH_POLICY_UPDATED"
    HEALTH_POLICY_DISABLED = "HEALTH_POLICY_DISABLED"
    HEALTH_POLICY_DELETED = "HEALTH_POLICY_DELETED"
    HEALTH_STATUS_CHANGED = "HEALTH_STATUS_CHANGED"
    OPENSHIFT_STATE_CHANGED = "OPENSHIFT_STATE_CHANGED"
    MAINTENANCE_ENABLED = "MAINTENANCE_ENABLED"
    MAINTENANCE_UPDATED = "MAINTENANCE_UPDATED"
    MAINTENANCE_DISABLED = "MAINTENANCE_DISABLED"
    MANAGER_CREATED = "MANAGER_CREATED"
    MANAGER_UPDATED = "MANAGER_UPDATED"
    SITE_CREATED = "SITE_CREATED"
    SITE_UPDATED = "SITE_UPDATED"


class AuditEvent(BaseModel):
    """One immutable, append-only record in the `audit_events` collection."""

    id: str = Field(alias="_id")
    event_type: EventType
    server_id: str | None = None  # None for rule/policy events; their id is in `data`
    actor: Actor
    request_id: str | None = None
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}
