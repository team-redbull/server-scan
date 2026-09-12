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

from dataclasses import dataclass
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
from app.domain.services.search import SORT_ACCESSORS, build_search_query, resolve_sort_field
from app.errors import NotFoundError, RevisionConflictError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import SERVERS_COLLECTION

_Document = dict[str, Any]


def _cursor_position_clause(
    *, sort_field: str, direction: int, position: CursorPosition
) -> dict[str, object]:
    """
    Build the `$or` clause selecting documents strictly past `position`.

    Either the sort field is strictly beyond the cursor's value, or it's
    tied and `_id` breaks the tie in the same direction. Both legs must
    agree with the query's own `.sort()` direction or the page would skip
    or repeat rows.

    A nullable sort field needs more than `$gt`/`$lt`, because Mongo sorts
    null before every string but its range operators are type-bracketed:
    `{$gt: null}` matches nothing at all and `{$lt: "abc"}` skips nulls
    entirely. Both would silently drop rows — see
    `docs/adr/0026-nullable-sort-fields.md`.

    Args:
        sort_field (str): The field being sorted on.
        direction (int): `1` for ascending, `-1` for descending.
        position (CursorPosition): The decoded cursor position.

    Returns:
        dict[str, object]: A Mongo filter clause to `$and` onto the query.
    """
    op = "$gt" if direction == 1 else "$lt"
    tie: dict[str, object] = {
        "$and": [{sort_field: position.sort_value}, {"_id": {op: position.id_value}}]
    }

    if position.sort_value is None:
        # Nulls sort first ascending, last descending. Everything
        # non-null is therefore still ahead of us going up, and nothing
        # is going down.
        ahead: list[dict[str, object]] = [{sort_field: {"$ne": None}}] if direction == 1 else []
        return {"$or": [*ahead, tie]}

    legs: list[dict[str, object]] = [{sort_field: {op: position.sort_value}}]
    if direction == -1:
        # Type bracketing leaves nulls out of `$lt`, but descending they
        # come after every string, so they are exactly what is left.
        legs.append({sort_field: None})
    legs.append(tie)
    return {"$or": legs}


@dataclass(frozen=True, slots=True)
class FacetRow:
    """
    One combination of filterable values and how many servers share it.

    Attributes:
        vendor (str | None): `identity.vendor`.
        source_provider (str | None): The collector that found them.
        installation_type (str | None): `classification.installation_type`.
        health_overall (str | None): `health.overall`.
        maintenance (bool): Whether maintenance is enabled.
        openshift_state (str | None): `openshift.lifecycle_state`.
        count (int): How many servers.
    """

    vendor: str | None
    source_provider: str | None
    installation_type: str | None
    health_overall: str | None
    maintenance: bool
    openshift_state: str | None
    count: int


class MongoServerRepository:
    """
    Implements `app.domain.ports.repository.ServerRepository`.

    Structural typing (the Protocol has no `register()`/ABC to inherit
    from) means this class satisfies the port by matching its method
    signatures, not by subclassing it.
    """

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

        The filter only matches the document a caller actually read
        (`_id` *and* the revision it saw), so a concurrent writer that
        already advanced the revision loses the race here instead of
        silently clobbering the other's write. No `upsert=True` — this
        never creates a document, so a filter that matches nothing is
        unconditionally a conflict, distinguished below by whether the
        document exists at all.

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

        # Fetch one extra document to detect `has_more` without a second
        # round trip.
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

    async def count(self, filters: dict[str, object]) -> int:
        """
        Count servers matching a Mongo filter document.

        Args:
            filters (dict[str, object]): The Mongo filter to count against.

        Returns:
            int: The number of matching servers.
        """
        return await self._collection.count_documents(dict(filters))

    async def facet_breakdown(
        self, *, filters: dict[str, object], search: str | None
    ) -> list[FacetRow]:
        """
        Per-combination server counts for one filtered view, in one round trip.

        A single `$group` over a composite key rather than a `$facet` with
        one sub-pipeline per dimension, for the same reason
        `site_breakdown` uses one: the key's cardinality is bounded by the
        enums and not by the estate — 4 vendors x 5 collectors x 4
        installation types x 6 severities x 2 maintenance states x 3
        OpenShift states, so about 5,700 small rows at absolute worst, and
        in practice a tiny fraction of that since most combinations never
        occur — and each dimension's counts are the marginals the caller
        sums out of them. The alternative is a `count_documents` per
        option, which is one round trip per number on the screen.

        `site_id` is deliberately not part of the key. It is a filter an
        operator has usually already applied by the time they want these
        numbers, and including it would multiply the rows by the site
        count to answer a question the site overview already answers.

        Args:
            filters (dict[str, object]): The same Mongo filter document
                `list_page` receives, so the counts describe exactly the
                page being looked at.
            search (str | None): The same search string, applied the same
                way.

        Returns:
            list[FacetRow]: One row per non-empty combination.
        """
        match: dict[str, object] = dict(filters)
        if search:
            match.update(build_search_query(search))

        pipeline: list[dict[str, Any]] = []
        if match:
            pipeline.append({"$match": match})
        pipeline.append(
            {
                "$group": {
                    "_id": {
                        "vendor": "$identity.vendor",
                        "source_provider": "$source_provider",
                        "installation_type": "$classification.installation_type",
                        "health_overall": "$health.overall",
                        "maintenance": "$maintenance.enabled",
                        "openshift_state": "$openshift.lifecycle_state",
                    },
                    "count": {"$sum": 1},
                }
            }
        )

        rows: list[FacetRow] = []
        async for doc in await self._collection.aggregate(pipeline):
            key = doc["_id"]
            rows.append(
                FacetRow(
                    vendor=key.get("vendor"),
                    source_provider=key.get("source_provider"),
                    installation_type=key.get("installation_type"),
                    health_overall=key.get("health_overall"),
                    maintenance=bool(key.get("maintenance")),
                    openshift_state=key.get("openshift_state"),
                    count=int(doc["count"]),
                )
            )
        return rows

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
                                # Missing and null both sort below any
                                # string in BSON, so a never-seen server
                                # is stale by this comparison.
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
        # Which fields to ignore depends on the row's own provider, so the
        # per-provider lists are folded into one `$switch` — which Mongo
        # rejects with zero branches, hence the literal empty list.
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

        Server counts for the whole estate, in one round trip. A single
        `$group` over every server rather than one count query
        per cell: the grouping key has a bounded cardinality (sites x 4
        vendors x 6 severities x 2 maintenance states x 4 installation
        types x 3 OpenShift states), so the four shipped sites plus the
        unassigned bucket give a few thousand small rows at absolute
        worst and far fewer in practice — and that stays bounded by the
        configured site count, never by the size of the estate. The caller pivots
        them in Python. The alternative — a `count_documents` per cell —
        would be that many round trips to build one screen.

        This is a full pass over the collection, which no index avoids for
        a grouping with no match stage. That is why the route in front of
        it caches: see `app.api.v1.sites`.

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
