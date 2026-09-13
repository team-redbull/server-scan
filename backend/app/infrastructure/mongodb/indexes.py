"""Declarative MongoDB index definitions.

Indexes are declared here, as data, rather than issued as ad-hoc
`create_index` calls scattered through repository code — `ensure_indexes`
is called once per process at startup (see `app.main`'s lifespan) and is
safe to call every time: `create_indexes` is idempotent when an index
already exists with an identical spec.

When a declared spec *has* changed, `ensure_indexes` drops the stored
index and recreates it, logging `mongo.index_respecified`. It originally
let MongoDB's `IndexKeySpecsConflict` propagate, on the reasoning that
drift is a bug worth surfacing — but that reasoning had the direction
backwards. The conflict is raised by the deployment applying the *new,
correct* spec, so propagating it means a corrected index takes the API
and every collector down on startup instead of migrating. Drift between
this file and a database is not a mystery to investigate; this file is
the declaration, and the database is what follows it. See
docs/adr/0016-redfish-standalone-collector.md, where correcting
`uniq_system_uuid` surfaced this.

Every compound index on `servers` ends in `_id` (ascending or descending
to match the leading field's sort direction) specifically to support
keyset pagination (`app.domain.services.cursor`): a `(filter_field,
sort_field, _id)` index lets `list_page`'s `$or` cursor-position query and
`.sort([(sort_field, dir), ("_id", dir)])` both use the same index instead
of falling back to an in-memory sort past the 32MB blocking-sort limit.

`uniq_vendor_serial` enforces the one identity constraint that actually
means "this is the same physical machine": ingestion's whole correlation
path (`IngestService._find_by_vendor_serial`) looks a server up by
`(vendor, serial_normalized)` alone, so a collision there is either a
genuine race between two concurrent ingests of the same server or a real
correlation bug — either way worth rejecting loudly
(`DuplicateKeyError`) at the database layer rather than silently
creating a duplicate document.

**`system_uuid` is deliberately NOT a unique index, as of 2026-09-09.**
It was one originally, on the same reasoning as `uniq_vendor_serial` —
but `system_uuid` is never used for correlation (only serial is), and a
live Cisco UCS domain proved the assumption wrong: `computeBlade`/
`computeRackUnit.uuid` reflects the *associated service profile's*
UUID, drawn from an admin-configured UUID Suffix Pool, not an immutable
hardware id — two cloned profiles, or two domains with overlapping
pool ranges, can legitimately hand two different real servers the same
UUID. Enforcing uniqueness on it made that vendor-side misconfiguration
a platform outage: the second server permanently failed to ingest
(`DuplicateKeyError` on every run) rather than the anomaly being merely
visible. The field is kept — it is real, useful, vendor-reported data,
indexed for exactly the kind of "which two documents share this UUID"
query that diagnosing this required — just no longer used to reject a
write.
"""

from __future__ import annotations

from typing import Any

import structlog
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import OperationFailure

logger = structlog.get_logger(__name__)

SERVERS_COLLECTION = "servers"
SITES_COLLECTION = "sites"
MANAGERS_COLLECTION = "managers"
CLASSIFICATION_RULES_COLLECTION = "classification_rules"
HEALTH_POLICIES_COLLECTION = "health_policies"
AUDIT_EVENTS_COLLECTION = "audit_events"

SERVER_INDEXES: list[IndexModel] = [
    IndexModel(
        [("identity.system_uuid", ASCENDING)],
        name="system_uuid",  # was "uniq_system_uuid"; retired below
        # `$type`, not `$exists`: the field is always present, often null.
        partialFilterExpression={"identity.system_uuid": {"$type": "string"}},
    ),
    IndexModel(
        [("identity.vendor", ASCENDING), ("identity.serial_normalized", ASCENDING)],
        name="uniq_vendor_serial",
        unique=True,
        # No `$ne` in partial filters; `$gt: ""` means non-empty (ADR-0026).
        partialFilterExpression={"identity.serial_normalized": {"$gt": ""}},
    ),
    IndexModel([("search_tokens", ASCENDING)], name="search_tokens"),
    # One per `search.FILTER_FIELDS` entry, ending in the default sort + `_id`.
    IndexModel(
        [("site_id", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="site_name_id",
    ),
    IndexModel(
        [("health.overall", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="health_name_id",
    ),
    IndexModel(
        [
            ("classification.installation_type", ASCENDING),
            ("name_normalized", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="installation_type_name_id",
    ),
    IndexModel(
        [("identity.vendor", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="vendor_name_id",
    ),
    IndexModel(
        [
            ("openshift.lifecycle_state", ASCENDING),
            ("name_normalized", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="openshift_state_name_id",
    ),
    # Also the membership jobs' working set (ADR-0024).
    IndexModel(
        [
            ("openshift.cluster_name", ASCENDING),
            ("name_normalized", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="openshift_cluster_name_id",
    ),
    IndexModel(
        [
            ("openshift.mce_name", ASCENDING),
            ("name_normalized", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="openshift_mce_name_id",
    ),
    IndexModel(
        [("maintenance.enabled", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="maintenance_enabled_name_id",
    ),
    IndexModel(
        [("source_provider", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="source_provider_name_id",
    ),
    # The fleet gauges' staleness query (ADR-0029).
    IndexModel(
        [("source_provider", ASCENDING), ("last_seen_at", ASCENDING)],
        name="source_provider_last_seen",
    ),
    IndexModel([("updated_at", DESCENDING), ("_id", DESCENDING)], name="updated_at_id"),
    # One `(field, _id)` per `SORT_FIELDS` entry, or unfiltered sorts COLLSCAN (ADR-0007).
    IndexModel([("last_seen_at", ASCENDING), ("_id", ASCENDING)], name="last_seen_at_id"),
    IndexModel([("name_normalized", ASCENDING), ("_id", ASCENDING)], name="name_id"),
    IndexModel([("identity.serial_normalized", ASCENDING), ("_id", ASCENDING)], name="serial_id"),
    IndexModel([("model_normalized", ASCENDING), ("_id", ASCENDING)], name="model_id"),
]

SITE_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
]

MANAGER_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
]

# Mirrors `evaluate.resolve_families`'s sort order (ADR-0026).
HEALTH_POLICY_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
    IndexModel(
        [
            ("enabled", ASCENDING),
            ("policy_key", ASCENDING),
            ("priority", DESCENDING),
            ("order", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="enabled_policy_key_priority_order_id",
    ),
    IndexModel([("policy_key", ASCENDING)], name="policy_key"),
    IndexModel([("category", ASCENDING)], name="category"),
    IndexModel([("scope.site_id", ASCENDING)], name="scope_site_id"),
]

# Mirrors `classification._sort_key` minus the specificity tiebreak (ADR-0026).
CLASSIFICATION_RULE_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
    IndexModel(
        [
            ("enabled", ASCENDING),
            ("priority", DESCENDING),
            ("order", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="enabled_priority_order_id",
    ),
    IndexModel([("scope.site_id", ASCENDING)], name="scope_site_id"),
    IndexModel([("scope.vendor", ASCENDING)], name="scope_vendor"),
    IndexModel([("scope.manager_type", ASCENDING)], name="scope_manager_type"),
]

# Every index ends in the fixed keyset sort `(created_at DESC, _id DESC)`.
AUDIT_EVENT_INDEXES: list[IndexModel] = [
    IndexModel([("created_at", DESCENDING), ("_id", DESCENDING)], name="created_at_id"),
    IndexModel(
        [("server_id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="server_id_created_at_id",
    ),
    IndexModel(
        [("event_type", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="event_type_created_at_id",
    ),
    IndexModel(
        [("actor.id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="actor_id_created_at_id",
    ),
]


_INDEX_KEY_SPECS_CONFLICT = 86
_INDEX_NOT_FOUND = 27

# Reconciliation is by name, so renaming or removing an index means adding
# its old name here, or a deployed database keeps enforcing it (ADR-0026).
RETIRED_INDEXES: dict[str, tuple[str, ...]] = {
    SERVERS_COLLECTION: ("uniq_system_uuid",),
    MANAGERS_COLLECTION: ("parent_manager_id",),  # field removed 2026-09-13
}


async def _create_indexes(
    db: AsyncDatabase[dict[str, Any]], collection: str, indexes: list[IndexModel]
) -> None:
    """
    Create a collection's declared indexes, replacing any changed ones.

    An `IndexKeySpecsConflict` is a changed declaration to migrate, not an
    error to propagate — see the module docstring.

    Args:
        db (AsyncDatabase[dict[str, Any]]): The database to act on.
        collection (str): Collection whose indexes are being ensured.
        indexes (list[IndexModel]): The declared indexes.

    Raises:
        OperationFailure: For any failure other than a specification
            conflict, which is left to surface rather than be retried
            blindly.
    """
    try:
        await db[collection].create_indexes(indexes)
    except OperationFailure as exc:
        if exc.code != _INDEX_KEY_SPECS_CONFLICT:
            raise
    else:
        return

    # One at a time, so one changed spec cannot drop the correct ones.
    for index in indexes:
        name = index.document.get("name")
        try:
            await db[collection].create_indexes([index])
        except OperationFailure as exc:
            if exc.code != _INDEX_KEY_SPECS_CONFLICT or not name:
                raise
            logger.warning(
                "mongo.index_respecified",
                collection=collection,
                index=name,
                hint=(
                    "The stored index specification differs from the declared one; "
                    "dropping and recreating it. Expect a brief window with no index."
                ),
            )
            await db[collection].drop_index(name)
            await db[collection].create_indexes([index])


async def _drop_retired(
    db: AsyncDatabase[dict[str, Any]], collection: str, names: tuple[str, ...]
) -> None:
    """
    Drop indexes this file no longer declares, if the database still has them.

    Args:
        db (AsyncDatabase[dict[str, Any]]): The database to act on.
        collection (str): Collection whose retired indexes are dropped.
        names (tuple[str, ...]): Index names that are no longer declared.

    Raises:
        OperationFailure: For any failure other than the index already
            being absent, which is the normal case.
    """
    for name in names:
        try:
            await db[collection].drop_index(name)
        except OperationFailure as exc:
            if exc.code != _INDEX_NOT_FOUND:
                raise
            continue
        logger.warning(
            "mongo.index_retired",
            collection=collection,
            index=name,
            hint="Dropped an index this build no longer declares.",
        )


async def ensure_indexes(db: AsyncDatabase[dict[str, Any]]) -> None:
    """
    Create every declared index if missing, and drop retired ones.

    Safe to call on every process startup — see the module docstring.

    Args:
        db (AsyncDatabase[dict[str, Any]]): The database to act on.
    """
    for collection, retired in RETIRED_INDEXES.items():
        await _drop_retired(db, collection, retired)
    await _create_indexes(db, SERVERS_COLLECTION, SERVER_INDEXES)
    await _create_indexes(db, SITES_COLLECTION, SITE_INDEXES)
    await _create_indexes(db, MANAGERS_COLLECTION, MANAGER_INDEXES)
    await _create_indexes(db, HEALTH_POLICIES_COLLECTION, HEALTH_POLICY_INDEXES)
    await _create_indexes(db, CLASSIFICATION_RULES_COLLECTION, CLASSIFICATION_RULE_INDEXES)
    await _create_indexes(db, AUDIT_EVENTS_COLLECTION, AUDIT_EVENT_INDEXES)
