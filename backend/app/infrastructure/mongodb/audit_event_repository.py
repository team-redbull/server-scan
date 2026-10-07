"""MongoDB implementation for the `audit_events` collection.

`record()` is the only write reachable from the API and UI: no `update`/`delete`
is exposed to a request. The two documented exceptions are
`rename_legacy_event_types` (startup) and `purge_before` (age-based retention,
ADR-0045). Only startup calls the first and only `tools/prune_events.py` the
second; a unit test asserts nothing else under `backend/app` references the purge.

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
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter
from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.audit_event import (
    LEGACY_EVENT_TYPES,
    AuditEvent,
    decode_legacy_event_type,
)
from app.domain.ports.repository import AuditStats
from app.domain.services.normalize import normalize_text
from app.errors import CursorInvalidError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import AUDIT_EVENTS_COLLECTION, SERVERS_COLLECTION
from app.utils.timeutil import utcnow

_Document = dict[str, Any]

_NAME_MATCH_ID_CAP = 5000
_ACTOR_LIMIT = 200
_MIN_PURGE_AGE = timedelta(days=7)


def _iso(value: datetime) -> str:
    """
    Render a filter bound the way `created_at` is stored (UTC ISO string, ADR-0006).

    Args:
        value (datetime): The bound; naive is taken as UTC.

    Returns:
        str: The stored-format string.
    """
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return TypeAdapter(datetime).dump_python(aware, mode="json")


@dataclass(frozen=True, slots=True)
class ActorSummary:
    """One distinct actor and how many events it recorded."""

    id: str
    type: str
    display: str | None
    event_count: int


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    """What an age cutoff would delete: how many events and their `created_at` span."""

    count: int
    oldest: str | None
    newest: str | None


@dataclass(slots=True)
class PurgeProgress:
    """Running totals of one `purge_before`, readable by the caller even if it raises midway."""

    deleted: int = 0
    batches: int = 0
    oldest: str | None = None
    newest: str | None = None


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """What one `purge_before` call deleted."""

    deleted: int
    batches: int
    truncated: bool
    oldest: str | None
    newest: str | None


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
    """MongoDB-backed store for append-only audit events (age-based retention aside, ADR-0045)."""

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

    async def rename_legacy_event_types(self) -> int:
        """
        Rewrite stored event types that have since been renamed (idempotent one-shot).

        The one deliberate exception to append-only: a rename of the type label, run at
        startup; readers also decode legacy names, so this is an optimisation, not a need.

        Returns:
            int: How many events were renamed.
        """
        renamed = 0
        for old, new in LEGACY_EVENT_TYPES.items():
            result = await self._collection.update_many(
                {"event_type": old}, {"$set": {"event_type": new}}
            )
            renamed += result.modified_count
        return renamed

    async def audit_stats(self) -> AuditStats:
        """
        Count the stored events and find the oldest one, for the audit-trail gauges.

        Returns:
            AuditStats: An estimated total (collection metadata, not a scan) and the oldest
                `created_at` string, `None` when the collection is empty.
        """
        total = await self._collection.estimated_document_count()
        oldest = await self._collection.find_one(
            {}, projection={"created_at": 1}, sort=[("created_at", 1), ("_id", 1)]
        )
        return AuditStats(total=total, oldest_created_at=oldest["created_at"] if oldest else None)

    async def preview_before(self, cutoff: datetime) -> RetentionPreview:
        """
        Count the events older than `cutoff` and report their oldest and newest `created_at`.

        Args:
            cutoff (datetime): Events strictly older than this would be purged.

        Returns:
            RetentionPreview: The count and span; both ends `None` when nothing matches.
        """
        query = {"created_at": {"$lt": _iso(cutoff)}}
        count = await self._collection.count_documents(query)
        if count == 0:
            return RetentionPreview(count=0, oldest=None, newest=None)
        oldest = await self._collection.find_one(query, sort=[("created_at", 1), ("_id", 1)])
        newest = await self._collection.find_one(query, sort=[("created_at", -1), ("_id", -1)])
        return RetentionPreview(
            count=count,
            oldest=oldest["created_at"] if oldest else None,
            newest=newest["created_at"] if newest else None,
        )

    async def purge_before(
        self,
        cutoff: datetime,
        *,
        batch_size: int,
        max_batches: int,
        progress: PurgeProgress | None = None,
    ) -> PurgeResult:
        """
        Delete events older than `cutoff`, oldest first, in bounded batches (ADR-0045).

        Args:
            cutoff (datetime): Events strictly older than this are deleted.
            batch_size (int): Events per delete.
            max_batches (int): Most batches one call may run.
            progress (PurgeProgress | None): Updated after every batch, so a caller can still
                account for what was deleted if a later batch raises.

        Returns:
            PurgeResult: Totals, whether the cap stopped it with events left, and the span.

        Raises:
            ValueError: If `cutoff` is within the last 7 days.
        """
        if cutoff > utcnow() - _MIN_PURGE_AGE:
            raise ValueError("refusing to purge audit events newer than 7 days")
        query = {"created_at": {"$lt": _iso(cutoff)}}
        progress = progress if progress is not None else PurgeProgress()
        truncated = False
        while True:
            if progress.batches >= max_batches:
                truncated = await self._collection.find_one(query, {"_id": 1}) is not None
                break
            docs = await (
                self._collection.find(query, {"created_at": 1})
                .sort([("created_at", 1), ("_id", 1)])
                .limit(batch_size)
                .to_list(length=batch_size)
            )
            if not docs:
                break
            result = await self._collection.delete_many(
                {"_id": {"$in": [d["_id"] for d in docs]}, **query}
            )
            progress.batches += 1
            progress.deleted += result.deleted_count
            progress.oldest = progress.oldest or docs[0]["created_at"]
            progress.newest = docs[-1]["created_at"]
            if len(docs) < batch_size:
                break
        return PurgeResult(
            deleted=progress.deleted,
            batches=progress.batches,
            truncated=truncated,
            oldest=progress.oldest,
            newest=progress.newest,
        )

    async def list_page(
        self,
        *,
        server_id: str | None = None,
        event_type: str | None = None,
        actor_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        server_name: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> AuditEventPage:
        """
        List audit events newest-first, optionally filtered and paginated.

        Args:
            server_id (str | None): If given, only events for this server.
            event_type (str | None): If given, only events of this type.
            actor_id (str | None): If given, only events by this actor.
            since (datetime | None): Inclusive lower bound on `created_at`.
            until (datetime | None): Exclusive upper bound on `created_at`.
            server_name (str | None): Case-insensitive substring of the server's
                name: events of a current server whose `name_normalized` contains
                it (at most 5000 ids are resolved), or whose snapshot
                `server_name` does, so pruned servers still match.
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
            current = decode_legacy_event_type(event_type)
            legacy = [old for old, new in LEGACY_EVENT_TYPES.items() if new == current]
            query["event_type"] = {"$in": [current, *legacy]} if legacy else current
        if actor_id is not None:
            query["actor.id"] = actor_id
        window: dict[str, str] = {}
        if since is not None:
            window["$gte"] = _iso(since)
        if until is not None:
            window["$lt"] = _iso(until)
        if window:
            query["created_at"] = window

        clauses: list[_Document] = []
        if server_name and (needle := normalize_text(server_name)):
            ids = await self._server_ids_by_name(needle)
            clauses.append(
                {
                    "$or": [
                        {"server_id": {"$in": ids}},
                        {"server_name": {"$regex": re.escape(needle), "$options": "i"}},
                    ]
                }
            )
        if cursor is not None:
            created_at_iso, event_id = _decode_cursor(cursor)
            clauses.append(
                {
                    "$or": [
                        {"created_at": {"$lt": created_at_iso}},
                        {"created_at": created_at_iso, "_id": {"$lt": event_id}},
                    ]
                }
            )
        if clauses:
            query["$and"] = clauses

        docs = await (
            self._collection.find(query)
            .sort([("created_at", -1), ("_id", -1)])
            .limit(page_size + 1)
            .to_list(length=page_size + 1)
        )
        has_more = len(docs) > page_size
        docs = docs[:page_size]
        items = [AuditEvent.model_validate(doc) for doc in docs]
        await self._backfill_server_names(items)

        # From the raw stored string, never `created_at.isoformat()`:
        # "+00:00" versus Pydantic's "Z" silently breaks the next page
        # (ADR-0006).
        next_cursor = (
            _encode_cursor(docs[-1]["created_at"], docs[-1]["_id"]) if has_more and docs else None
        )
        return AuditEventPage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def _server_ids_by_name(self, needle: str) -> list[str]:
        """
        Resolve the ids of current servers whose normalized name contains `needle`.

        Args:
            needle (str): Normalized text.

        Returns:
            list[str]: Up to 5000 server ids.
        """
        cursor = self._mongo.db[SERVERS_COLLECTION].find(
            {"name_normalized": {"$regex": re.escape(needle)}}, {"_id": 1}
        )
        return [d["_id"] for d in await cursor.to_list(length=_NAME_MATCH_ID_CAP)]

    async def _backfill_server_names(self, items: list[AuditEvent]) -> None:
        """
        Fill `server_name` on legacy events from the current servers, in one query.

        Args:
            items (list[AuditEvent]): A page of events, mutated in place.
        """
        missing = {e.server_id for e in items if e.server_name is None and e.server_id}
        if not missing:
            return
        cursor = self._mongo.db[SERVERS_COLLECTION].find(
            {"_id": {"$in": list(missing)}}, {"name": 1}
        )
        names = {d["_id"]: d.get("name") async for d in cursor}
        for e in items:
            if e.server_name is None and e.server_id:
                e.server_name = names.get(e.server_id)

    async def list_actors(self) -> list[ActorSummary]:
        """
        Distinct actors by `actor.id`, busiest first, at most 200.

        Type and display come from each actor's most recent event.

        Returns:
            list[ActorSummary]: One row per actor.
        """
        pipeline: list[_Document] = [
            {"$sort": {"created_at": -1, "_id": -1}},
            {"$group": {"_id": "$actor.id", "actor": {"$first": "$actor"}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1, "_id": 1}},
            {"$limit": _ACTOR_LIMIT},
        ]
        cursor = await self._collection.aggregate(pipeline)
        return [
            ActorSummary(
                id=row["_id"],
                type=row["actor"].get("type", "SYSTEM"),
                display=row["actor"].get("display"),
                event_count=row["n"],
            )
            async for row in cursor
        ]

    async def count_by_provider_since(self, event_type: str, since: datetime) -> dict[str, int]:
        """
        Count events of one type since a cutoff, grouped by `data.source_provider`.

        Args:
            event_type (str): The `event_type` to count.
            since (datetime): Inclusive lower bound on `created_at`.

        Returns:
            dict[str, int]: Count per provider; events without one land under `unknown`.
        """
        cutoff = TypeAdapter(datetime).dump_python(since, mode="json")
        pipeline = [
            {"$match": {"event_type": event_type, "created_at": {"$gte": cutoff}}},
            {"$group": {"_id": "$data.source_provider", "n": {"$sum": 1}}},
        ]
        cursor = await self._collection.aggregate(pipeline)
        return {(row["_id"] or "unknown"): row["n"] async for row in cursor}
