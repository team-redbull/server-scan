"""MongoDB implementation for the `audit_events` collection.

`record()` is the only write method on this class — deliberately no
`update`/`delete` of any kind. That is what makes "audit events are
immutable" a structural property of the codebase rather than a policy
someone has to remember: nothing in this codebase *can* call
`update_one`/`delete_one` against this collection, because no method here
exposes that capability.

Pagination is a simpler keyset cursor than `app.domain.services.cursor`'s
HMAC-signed one for `servers`: the sort order here is always
`(created_at DESC, _id DESC)` — there is no per-request sort choice to
bind the cursor to — and a forged/stale audit-event cursor has no
consequence worse than seeing the wrong page of a read-only log, unlike a
tampered server-list cursor which that module's signature guards against
reaching an unintended filter. Simpler, unsigned encoding here is a
deliberate proportionality choice, not an oversight.

Every repository in this codebase persists `datetime` fields via
`model_dump(..., mode="json")`, which serializes them to ISO 8601
*strings* — so `created_at` is stored in MongoDB as a string, not a native
BSON Date. The cursor's `created_at` component is therefore kept as that
same ISO string end to end, including in the `$or` query below: comparing
a native Python `datetime` (a different BSON type) against a
string-stored field would silently produce wrong `$lt` results — BSON
compares by type first, so a cross-type comparison isn't the value
comparison it looks like. ISO 8601 strings (zero-padded, `Z`-suffixed)
sort lexicographically identically to chronological order, which is
exactly what makes staying in string form here both correct and free —
`_decode_cursor` still parses the string with `fromisoformat` once, purely
to validate it's a real timestamp before trusting client input.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.audit_event import AuditEvent
from app.errors import CursorInvalidError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import AUDIT_EVENTS_COLLECTION

_Document = dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuditEventPage:
    """One page of audit events plus the cursor to fetch the next page."""

    items: list[AuditEvent]
    next_cursor: str | None
    has_more: bool


def _encode_cursor(created_at_iso: str, event_id: str) -> str:
    payload = json.dumps([created_at_iso, event_id])
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """
    Decode and validate an audit-event page cursor.

    Args:
        cursor (str): The opaque cursor from a previous `list_page` call.

    Returns:
        tuple[str, str]: `(created_at_iso, event_id)`, the ISO string as
            stored, not a parsed `datetime` (see the module docstring on
            why the query must stay in string form).

    Raises:
        CursorInvalidError: If the cursor is malformed or its timestamp
            is not a timezone-aware ISO 8601 string.
    """
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        created_at_iso, event_id = json.loads(payload)
        if not isinstance(created_at_iso, str) or not isinstance(event_id, str):
            raise TypeError("cursor payload has the wrong shape")
        parsed = datetime.fromisoformat(created_at_iso)  # validation only; result unused
        if parsed.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise CursorInvalidError("Malformed event cursor.", details={"cursor": cursor}) from exc
    else:
        return created_at_iso, event_id


class MongoAuditEventRepository:
    """MongoDB-backed store for immutable audit events."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[AUDIT_EVENTS_COLLECTION]

    async def record(self, event: AuditEvent) -> AuditEvent:
        """
        Insert one audit event.

        Args:
            event (AuditEvent): The event to persist.

        Returns:
            AuditEvent: The same event, for chaining.
        """
        doc = event.model_dump(by_alias=True, mode="json")
        await self._collection.insert_one(doc)
        return event

    async def list_page(
        self,
        *,
        server_id: str | None = None,
        event_type: str | None = None,
        actor_id: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> AuditEventPage:
        """
        List audit events newest-first, optionally filtered and paginated.

        Args:
            server_id (str | None): If given, only events for this server.
            event_type (str | None): If given, only events of this type.
            actor_id (str | None): If given, only events by this actor.
            cursor (str | None): If given, resume after this page's last
                item (from a previous call's `next_cursor`).
            page_size (int): Maximum number of items to return.

        Returns:
            AuditEventPage: The matching page, its `next_cursor` (`None`
                if this is the last page), and whether more remain.
        """
        query: dict[str, object] = {}
        if server_id is not None:
            query["server_id"] = server_id
        if event_type is not None:
            query["event_type"] = event_type
        if actor_id is not None:
            query["actor.id"] = actor_id

        if cursor is not None:
            created_at_iso, event_id = _decode_cursor(cursor)
            query["$or"] = [
                {"created_at": {"$lt": created_at_iso}},
                {"created_at": created_at_iso, "_id": {"$lt": event_id}},
            ]

        docs = await (
            self._collection.find(query)
            .sort([("created_at", -1), ("_id", -1)])
            .limit(page_size + 1)
            .to_list(length=page_size + 1)
        )
        has_more = len(docs) > page_size
        docs = docs[:page_size]
        items = [AuditEvent.model_validate(doc) for doc in docs]

        # From the raw stored string, never `created_at.isoformat()`:
        # "+00:00" versus Pydantic's "Z" silently breaks the next page
        # (ADR-0006).
        next_cursor = (
            _encode_cursor(docs[-1]["created_at"], docs[-1]["_id"]) if has_more and docs else None
        )
        return AuditEventPage(items=items, next_cursor=next_cursor, has_more=has_more)
