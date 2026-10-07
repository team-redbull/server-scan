"""The `audit_events` collection: an append-only log of every platform mutation.

Append-only from every request path: `record()` is the only write the API
can reach. The two documented exceptions in `MongoAuditEventRepository` are
`rename_legacy_event_types` (startup) and the age-based retention purge, called
only from `tools/prune_events.py` (docs/adr/0045).

`EventType` is a closed, append-only registry (mirroring `ErrorCode`'s own
convention in `app.errors`): new values are added at the end, existing
values are never renumbered or removed, since stored events reference them
by string and old events must stay readable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ActorType(StrEnum):
    """The kind of principal that performed an audited action."""

    SYSTEM = "SYSTEM"
    USER = "USER"
    TOKEN = "TOKEN"  # noqa: S105 - an actor-type label, not a credential


class Role(StrEnum):
    """An authenticated caller's permission level; see docs/adr/0034 for `NO_PERMISSION`.

    `AUDITOR` (docs/adr/0043) is read-only like `VIEWER` but may also read the audit trail;
    only the static `api_token_auditor` resolves to it, never an AD login.
    """

    ADMIN = "ADMIN"
    VIEWER = "VIEWER"
    AUDITOR = "AUDITOR"


class Actor(BaseModel):
    """The principal an `AuditEvent` records as having performed it."""

    type: ActorType
    id: str
    display: str | None = None
    # None for the SYSTEM actor and for every actor recorded before
    # docs/adr/0034 — an old stored event still decodes.
    role: Role | None = None


class EventType(StrEnum):
    """The closed, append-only registry of audit event kinds."""

    SERVER_CREATED = "SERVER_CREATED"
    SERVER_UPDATED = "SERVER_UPDATED"
    SERVER_DELETED = "SERVER_DELETED"
    SERVER_PRUNED = "SERVER_PRUNED"
    CLASSIFICATION_CHANGED = "CLASSIFICATION_CHANGED"
    CLASSIFICATION_RULE_CREATED = "CLASSIFICATION_RULE_CREATED"
    CLASSIFICATION_RULE_UPDATED = "CLASSIFICATION_RULE_UPDATED"
    CLASSIFICATION_RULE_DELETED = "CLASSIFICATION_RULE_DELETED"
    HEALTH_POLICY_CREATED = "HEALTH_POLICY_CREATED"
    HEALTH_POLICY_UPDATED = "HEALTH_POLICY_UPDATED"
    HEALTH_POLICY_DISABLED = "HEALTH_POLICY_DISABLED"
    HEALTH_POLICY_DELETED = "HEALTH_POLICY_DELETED"
    HEALTH_CHANGED = "HEALTH_CHANGED"
    OPENSHIFT_STATE_CHANGED = "OPENSHIFT_STATE_CHANGED"
    SERVER_RESERVED = "SERVER_RESERVED"
    SERVER_RESERVATION_REFUSED = "SERVER_RESERVATION_REFUSED"
    SERVER_RELEASED = "SERVER_RELEASED"
    MAINTENANCE_ENABLED = "MAINTENANCE_ENABLED"
    MAINTENANCE_UPDATED = "MAINTENANCE_UPDATED"
    MAINTENANCE_DISABLED = "MAINTENANCE_DISABLED"
    MANAGER_CREATED = "MANAGER_CREATED"
    MANAGER_UPDATED = "MANAGER_UPDATED"
    SITE_CREATED = "SITE_CREATED"
    AUDIT_PURGED = "AUDIT_PURGED"
    SITE_UPDATED = "SITE_UPDATED"


LEGACY_EVENT_TYPES = {"HEALTH_STATUS_CHANGED": EventType.HEALTH_CHANGED.value}


def decode_legacy_event_type(value: object) -> object:
    """
    Map an event type renamed since it was stored onto its current name.

    Args:
        value (object): The stored or caller-supplied event type.

    Returns:
        object: The current name if `value` is a legacy one, else `value`.
    """
    if isinstance(value, str):
        return LEGACY_EVENT_TYPES.get(value, value)
    return value


class AuditEvent(BaseModel):
    """One append-only record in the `audit_events` collection (age-based retention aside)."""

    id: str = Field(alias="_id")
    event_type: EventType
    server_id: str | None = None  # None for rule/policy events; their id is in `data`
    # The name at write time, so an event outlives a rename or a prune. None on legacy events.
    server_name: str | None = None
    actor: Actor
    request_id: str | None = None
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    _decode = field_validator("event_type", mode="before")(decode_legacy_event_type)
