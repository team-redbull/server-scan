"""MongoDB implementation of the `ServerRepository` port.

Keyset (not skip/limit) pagination throughout: `list_page` sorts on
`(sort_field, _id)` and, when a cursor is present, adds an `$or` clause
positioned just past `(cursor.sort_value, cursor.id_value)` rather than
using `.skip(n)`, which degrades linearly with offset and can return
duplicate/missing rows under concurrent writes. See
`app.domain.services.cursor` for the cursor's signing/binding contract and
`app.infrastructure.mongodb.indexes` for the compound indexes this leans
on to stay an IXSCAN at every page.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.server import Server
from app.domain.ports.repository import (
    ClusterSnapshotRow,
    FleetSnapshot,
    Page,
    ProviderSnapshotRow,
    SiteBreakdownRow,
)
from app.domain.services.cursor import CursorPosition, decode_cursor, encode_cursor
from app.domain.services.search import (
    SORT_ACCESSORS,
    build_search_query,
    resolve_sort_field,
)
from app.errors import NotFoundError, RevisionConflictError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import SERVERS_COLLECTION

_Document = dict[str, Any]


def _stored_form(value: str | datetime | None) -> str | None:
    """
    Render a cursor's sort value the way the document stores it.

    Args:
        value (str | datetime | None): The decoded cursor sort value.

    Returns:
        str | None: The value as stored — a datetime as its ISO 8601 string.
    """
    if isinstance(value, datetime):
        return TypeAdapter(datetime).dump_python(value, mode="json")
    return value


def _cursor_position_clause(
    *, sort_field: str, direction: int, position: CursorPosition
) -> dict[str, object]:
    """
    Build the `$or` clause selecting documents strictly past `position`.

    Both legs must agree with the query's own `.sort()` direction, and a
    nullable sort field needs the null-aware branches (ADR-0026).

    Args:
        sort_field (str): The field being sorted on.
        direction (int): `1` for ascending, `-1` for descending.
        position (CursorPosition): The decoded cursor position.

    Returns:
        dict[str, object]: A Mongo filter clause to `$and` onto the query.
    """
    op = "$gt" if direction == 1 else "$lt"
    # Stored as ISO strings; a real datetime in `$gt` matches nothing (ADR-0006).
    value = _stored_form(position.sort_value)
    tie: dict[str, object] = {"$and": [{sort_field: value}, {"_id": {op: position.id_value}}]}

    if value is None:
        # Nulls sort first ascending: everything non-null is still ahead.
        ahead: list[dict[str, object]] = [{sort_field: {"$ne": None}}] if direction == 1 else []
        return {"$or": [*ahead, tie]}

    legs: list[dict[str, object]] = [{sort_field: {op: value}}]
    if direction == -1:
        # `$lt` skips nulls, and descending they are exactly what is left.
        legs.append({sort_field: None})
    legs.append(tie)
    return {"$or": legs}


_ROW_PROJECTION: dict[str, int] = {
    "name": 1,
    "identity.vendor": 1,
    "identity.serial": 1,
    "model": 1,
    "site_id": 1,
    "source_provider": 1,
    "classification.installation_type": 1,
    "health.overall": 1,
    "maintenance.enabled": 1,
    "maintenance.reason": 1,
    "openshift.lifecycle_state": 1,
    "openshift.cluster_name": 1,
    "openshift.mce_name": 1,
    "last_seen_at": 1,
    "updated_at": 1,
    "reachable": 1,
    "network.bmc.host": 1,
    "network.interfaces.mac": 1,
}


class MongoServerRepository:
    """Implements `app.domain.ports.repository.ServerRepository`, structurally."""

    def __init__(self, mongo: MongoClientHolder, *, cursor_secret: str) -> None:
        """
        Store the shared Mongo client holder and the cursor-signing secret.

        Args:
            mongo (MongoClientHolder): The connected client holder.
            cursor_secret (str): HMAC secret used to sign/verify pagination
                cursors (see `app.domain.services.cursor`).
        """
        self._mongo = mongo
        self._cursor_secret = cursor_secret

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[SERVERS_COLLECTION]

    async def upsert(self, server: Server) -> Server:
        """
        Replace-or-insert a server by `_id`.

        Args:
            server (Server): The server to persist.

        Returns:
            Server: The same server, for chaining.

        Raises:
            pymongo.errors.DuplicateKeyError: If the document collides with
                an *other* document on the one secondary unique index,
                `(identity.vendor, identity.serial_normalized)` —
                uncaught; that is expected and is `app.application.
                services.ingest`'s job to catch and resolve via
                lookup+update, not this repository's. `identity.
                system_uuid` cannot raise this: it is indexed but not
                unique, since 2026-09-09 (see `app.infrastructure.
                mongodb.indexes`'s module docstring).
        """
        doc = server.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": server.id}, doc, upsert=True)
        return server

    async def upsert_with_revision_check(self, server: Server, *, expected_revision: int) -> Server:
        """
        Compare-and-set a server on its `revision`.

        The filter matches `_id` *and* the revision the caller read, so a
        concurrent writer loses the race instead of clobbering. Never creates.

        Args:
            server (Server): The server to persist, as read plus changes.
            expected_revision (int): The revision the caller last read.

        Returns:
            Server: The same server, for chaining.

        Raises:
            NotFoundError: If no document with this `_id` exists at all.
            RevisionConflictError: If the document exists but its stored
                revision no longer matches `expected_revision`.
        """
        doc = server.model_dump(by_alias=True, mode="json")
        result = await self._collection.replace_one(
            {"_id": server.id, "revision": expected_revision}, doc
        )
        if result.matched_count == 0:
            current = await self._collection.find_one(
                {"_id": server.id}, projection={"revision": 1}
            )
            if current is None:
                raise NotFoundError(
                    f"No server with id {server.id!r}.", details={"server_id": server.id}
                )
            raise RevisionConflictError(
                f"Server {server.id!r} was modified concurrently: expected revision "
                f"{expected_revision}, stored revision is now {current['revision']}.",
                current_revision=current["revision"],
            )
        return server

    async def get_by_id(self, server_id: str) -> Server | None:
        """
        Look up one server by its id.

        Args:
            server_id (str): The server's id.

        Returns:
            Server | None: The server, or None if not found.
        """
        doc = await self._collection.find_one({"_id": server_id})
        if doc is None:
            return None
        return Server.model_validate(doc)

    async def list_page(
        self,
        *,
        filters: dict[str, object],
        search: str | None,
        sort: str,
        sort_desc: bool,
        cursor: str | None,
        page_size: int,
        with_count: bool,
    ) -> Page:
        """
        List one keyset page of servers matching filters/search.

        Args:
            filters (dict[str, object]): A Mongo filter document (already
                whitelisted by `app.domain.services.search.FILTER_FIELDS`).
            search (str | None): Free-text search string, or None.
            sort (str): The sort field name to resolve via
                `resolve_sort_field`.
            sort_desc (bool): Whether to sort descending.
            cursor (str | None): An opaque cursor from a previous page's
                `next_cursor`, or None for the first page.
            page_size (int): Maximum number of items to return.
            with_count (bool): Whether to also compute `total_count`
                (a separate `count_documents` round trip).

        Returns:
            Page: The matching items, the next cursor (`None` if this is
                the last page), whether more remain, and the total count
                if requested.
        """
        sort_field = resolve_sort_field(sort)
        direction = -1 if sort_desc else 1

        base_filter: dict[str, object] = dict(filters)
        if search:
            base_filter.update(build_search_query(search))

        query_filter: dict[str, object] = base_filter
        if cursor:
            position = decode_cursor(
                cursor,
                filters=filters,
                sort=sort,
                sort_desc=sort_desc,
                page_size=page_size,
                secret=self._cursor_secret,
            )
            cursor_clause = _cursor_position_clause(
                sort_field=sort_field, direction=direction, position=position
            )
            query_filter = {"$and": [base_filter, cursor_clause]} if base_filter else cursor_clause

        raw_docs = await (
            self._collection.find(query_filter)
            .sort([(sort_field, direction), ("_id", direction)])
            .limit(page_size + 1)
            .to_list(length=page_size + 1)
        )

        has_more = len(raw_docs) > page_size
        items = [Server.model_validate(doc) for doc in raw_docs[:page_size]]

        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(
                sort_value=SORT_ACCESSORS[sort](last),
                id_value=last.id,
                filters=filters,
                sort=sort,
                sort_desc=sort_desc,
                page_size=page_size,
                secret=self._cursor_secret,
            )

        total_count: int | None = None
        if with_count:
            total_count = await self._collection.count_documents(base_filter)

        return Page(
            items=items, next_cursor=next_cursor, has_more=has_more, total_count=total_count
        )

    async def find_one_by_name(
        self, name_normalized: str, *, filters: dict[str, object]
    ) -> Server | None:
        """
        Look up one server by its normalized name, case-insensitively (ADR-0032).

        Args:
            name_normalized (str): The caller's name, already run through
                `app.domain.services.normalize.normalize_text`.
            filters (dict[str, object]): Extra whitelisted filters
                (`vendor`/`source_provider`) to combine with the name match.

        Returns:
            Server | None: The matching document, or `None`.
        """
        doc = await self._collection.find_one({**filters, "name_normalized": name_normalized})
        return Server.model_validate(doc) if doc is not None else None

    async def sample_available_tier(
        self,
        *,
        filters: dict[str, object],
        severity: str,
        size: int,
        exclude_ids: Sequence[str] = (),
    ) -> list[Server]:
        """
        Randomly draw up to `size` servers matching `filters` at one health severity (ADR-0032).

        Args:
            filters (dict[str, object]): Name/vendor/source_provider and
                assignability filters, without a `health.overall` clause.
            severity (str): The `HealthSeverity` value this draw is
                restricted to.
            size (int): Maximum candidates to draw.
            exclude_ids (Sequence[str]): `_id`s already drawn and rejected,
                excluded from this draw so a replacement round doesn't
                repeat them.

        Returns:
            list[Server]: Up to `size` randomly drawn matching servers.
        """
        if size <= 0:
            return []
        match: dict[str, object] = {**filters, "health.overall": severity}
        if exclude_ids:
            match["_id"] = {"$nin": list(exclude_ids)}
        pipeline: list[dict[str, Any]] = [{"$match": match}, {"$sample": {"size": size}}]
        docs = await (await self._collection.aggregate(pipeline)).to_list(length=size)
        return [Server.model_validate(doc) for doc in docs]

    async def list_rows(self) -> list[dict[str, Any]]:
        """
        Every server, projected to the inventory-row fields (ADR-0033).

        A projection, not `Server.model_validate`: validating 2,504 full
        documents measured 544 ms, the projection 16 ms.

        Returns:
            list[dict[str, Any]]: Raw projected documents, `_id` included.
        """
        cursor = self._collection.find({}, _ROW_PROJECTION).sort("name_normalized", 1)
        return await cursor.to_list(length=None)

    async def count(self, filters: dict[str, object]) -> int:
        """
        Count servers matching a Mongo filter document.

        Args:
            filters (dict[str, object]): The Mongo filter to count against.

        Returns:
            int: The number of matching servers.
        """
        return await self._collection.count_documents(dict(filters))

    async def fleet_snapshot(self, *, stale_before: datetime) -> FleetSnapshot:
        """
        Summarise the fleet for the Prometheus gauges (ADR-0029).

        One `$facet` pass; the cutoff is rendered like the stored strings (ADR-0006).

        Args:
            stale_before (datetime): A server whose `last_seen_at` is older
                than this — or absent — counts as stale.

        Returns:
            FleetSnapshot: Per-collector, per-cluster and per-health counts.
        """
        cutoff = TypeAdapter(datetime).dump_python(stale_before, mode="json")
        pipeline: list[dict[str, Any]] = [
            {
                "$facet": {
                    "by_provider": [
                        {
                            "$group": {
                                "_id": "$source_provider",
                                "total": {"$sum": 1},
                                # Missing sorts below any string in BSON,
                                # so a never-seen server is stale.
                                "stale": {
                                    "$sum": {"$cond": [{"$lt": ["$last_seen_at", cutoff]}, 1, 0]}
                                },
                                "unreachable": {
                                    "$sum": {"$cond": [{"$eq": ["$reachable", False]}, 1, 0]}
                                },
                                "last_seen_at": {"$max": "$last_seen_at"},
                            }
                        }
                    ],
                    "by_cluster": [
                        {"$match": {"openshift.cluster_name": {"$type": "string"}}},
                        {
                            "$group": {
                                "_id": "$openshift.cluster_name",
                                "held": {"$sum": 1},
                                "last_reported_at": {"$max": "$openshift.last_reported_at"},
                            }
                        },
                    ],
                    "unread_by_field": [
                        {"$unwind": "$unread_fields"},
                        {
                            "$group": {
                                "_id": {"provider": "$source_provider", "field": "$unread_fields"},
                                "count": {"$sum": 1},
                            }
                        },
                    ],
                    "by_health": [{"$group": {"_id": "$health.overall", "count": {"$sum": 1}}}],
                    "by_policy": [
                        {"$unwind": "$health.active_policy_keys"},
                        {"$group": {"_id": "$health.active_policy_keys", "count": {"$sum": 1}}},
                    ],
                    "in_maintenance": [
                        {"$match": {"maintenance.enabled": True}},
                        {"$count": "count"},
                    ],
                    "duplicate_names": [
                        {"$group": {"_id": "$name", "count": {"$sum": 1}}},
                        {"$match": {"count": {"$gt": 1}}},
                    ],
                }
            }
        ]
        facets = await (await self._collection.aggregate(pipeline)).to_list(length=1)
        result = facets[0] if facets else {}
        totals = {row["_id"]: int(row["total"]) for row in result.get("by_provider", [])}
        partial = await self._partial_counts(totals, result.get("unread_by_field", []))
        return FleetSnapshot(
            by_provider=[
                ProviderSnapshotRow(
                    source_provider=row["_id"],
                    total=int(row["total"]),
                    stale=int(row["stale"]),
                    unreachable=int(row["unreachable"]),
                    partial=partial.get(row["_id"], 0),
                    last_seen_at=row.get("last_seen_at"),
                )
                for row in result.get("by_provider", [])
            ],
            by_cluster=[
                ClusterSnapshotRow(
                    cluster_name=str(row["_id"]),
                    held=int(row["held"]),
                    last_reported_at=row.get("last_reported_at"),
                )
                for row in result.get("by_cluster", [])
            ],
            by_health={
                str(row["_id"]): int(row["count"])
                for row in result.get("by_health", [])
                if row["_id"] is not None
            },
            by_policy={str(row["_id"]): int(row["count"]) for row in result.get("by_policy", [])},
            in_maintenance=int(next(iter(result.get("in_maintenance", [])), {}).get("count", 0)),
            duplicate_name_groups=len(result.get("duplicate_names", [])),
            duplicate_name_servers=sum(
                int(row["count"]) for row in result.get("duplicate_names", [])
            ),
        )

    async def _partial_counts(
        self, totals: dict[Any, int], unread_by_field: list[dict[str, Any]]
    ) -> dict[Any, int]:
        """
        Count servers per collector with an unread field that collector CAN read.

        A field unread on every one of a collector's servers is structural —
        the collector never reports it — and is ignored (ADR-0029).

        Args:
            totals (dict[Any, int]): Servers per `source_provider`.
            unread_by_field (list[dict[str, Any]]): The facet's per-(provider,
                field) unread counts.

        Returns:
            dict[Any, int]: Partial-read servers per `source_provider`.
        """
        structural: dict[Any, list[str]] = {}
        for row in unread_by_field:
            provider, field = row["_id"]["provider"], row["_id"]["field"]
            if int(row["count"]) >= totals.get(provider, 0):
                structural.setdefault(provider, []).append(field)
        if not totals:
            return {}
        # Mongo rejects a `$switch` with zero branches, hence the literal.
        ignored: Any = (
            {
                "$switch": {
                    "branches": [
                        {"case": {"$eq": ["$source_provider", provider]}, "then": fields}
                        for provider, fields in structural.items()
                    ],
                    "default": [],
                }
            }
            if structural
            else []
        )
        pipeline: list[dict[str, Any]] = [
            {"$match": {"unread_fields": {"$exists": True, "$ne": []}}},
            {
                "$project": {
                    "source_provider": 1,
                    "real": {"$setDifference": ["$unread_fields", ignored]},
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$real"}, 0]}}},
            {"$group": {"_id": "$source_provider", "count": {"$sum": 1}}},
        ]
        return {
            row["_id"]: int(row["count"])
            async for row in await self._collection.aggregate(pipeline)
        }

    async def site_breakdown(self) -> list[SiteBreakdownRow]:
        """
        Per-(site, vendor, health, maintenance, installation, OpenShift) counts.

        One `$group` over every server — a full collection pass, which is
        why `app.api.v1.sites` caches it (docs/architecture.md, "caching").

        Returns:
            list[SiteBreakdownRow]: One row per non-empty combination.
        """
        pipeline: list[dict[str, Any]] = [
            {
                "$group": {
                    "_id": {
                        "site_id": "$site_id",
                        "vendor": "$identity.vendor",
                        "health": "$health.overall",
                        "maintenance": "$maintenance.enabled",
                        "installation_type": "$classification.installation_type",
                        "openshift_state": "$openshift.lifecycle_state",
                    },
                    "count": {"$sum": 1},
                }
            }
        ]
        rows: list[SiteBreakdownRow] = []
        async for doc in await self._collection.aggregate(pipeline):
            key = doc["_id"]
            rows.append(
                SiteBreakdownRow(
                    site_id=key.get("site_id"),
                    vendor=key.get("vendor"),
                    health=key.get("health"),
                    maintenance=bool(key.get("maintenance")),
                    installation_type=key.get("installation_type"),
                    openshift_state=key.get("openshift_state"),
                    count=int(doc["count"]),
                )
            )
        return rows
