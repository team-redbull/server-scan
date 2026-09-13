"""Search/filter/sort whitelists for `GET /api/v1/servers`.

Every filter key, sort field, and the search string itself is validated
against an explicit whitelist before it ever reaches a Mongo query —
nothing here accepts an arbitrary field path or builds a query fragment
from a caller-supplied key. `app.infrastructure.mongodb.server_repository`
is the only consumer of `build_filter_query`/`resolve_sort_field`/
`build_search_query`; both `filters` and `sort` on
`ServerRepository.list_page` accept the raw (whitelist-key) form the API
layer receives — the repository is where the translation to real Mongo
field paths happens, via this module, so query-shape logic lives in one
place regardless of whether it's reached through a filter, a sort, or a
cursor.

Search itself is never raw regex against arbitrary user input: it's an
escaped, anchored-prefix match against the multikey-indexed
`search_tokens` field built by `app.domain.services.search_tokens`. An
anchored (`^`), escaped (`re.escape`) prefix is the one shape of user
input that's both safe (no ReDoS, no injection) and index-friendly (a
prefix regex against a sorted multikey index reduces to a bounded range
scan, not a collection scan).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from app.domain.models.server import Server
from app.domain.value_objects.site import UNASSIGNED_SITE_ID
from app.errors import (
    SearchQueryTooLongError,
    SearchQueryTooShortError,
    UnknownFilterError,
    UnknownSortFieldError,
)

# Query-param name -> Mongo path, explicit on purpose — see
# docs/architecture.md, "Search, pagination, and caching".
FILTER_FIELDS: dict[str, str] = {
    "site_id": "site_id",
    "vendor": "identity.vendor",
    "manager_id": "manager_id",
    "installation_type": "classification.installation_type",
    "health_overall": "health.overall",
    "maintenance": "maintenance.enabled",
    "source_provider": "source_provider",
    "openshift_state": "openshift.lifecycle_state",
    "cluster_name": "openshift.cluster_name",
}

SORT_FIELDS: dict[str, str] = {
    "name": "name_normalized",
    "serial": "identity.serial_normalized",
    "model": "model_normalized",
    "updated_at": "updated_at",
    "last_seen_at": "last_seen_at",
    "openshift_state": "openshift.lifecycle_state",
    # Both nullable — `_cursor_position_clause` is null-aware (ADR-0026).
    "cluster_name": "openshift.cluster_name",
    "mce_name": "openshift.mce_name",
}

MIN_SEARCH_QUERY_LENGTH = 2
MAX_SEARCH_QUERY_LENGTH = 64

# `last_seen_at` falls back to the epoch: a cursor position must be concrete.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

SORT_ACCESSORS: dict[str, Callable[[Server], str | datetime | None]] = {
    "name": lambda s: s.name_normalized,
    "serial": lambda s: s.identity.serial_normalized,
    "model": lambda s: s.model_normalized,
    "updated_at": lambda s: s.updated_at,
    "last_seen_at": lambda s: s.last_seen_at or _EPOCH,
    "openshift_state": lambda s: s.openshift.lifecycle_state.value,
    "cluster_name": lambda s: s.openshift.cluster_name,
    "mce_name": lambda s: s.openshift.mce_name,
}


STALE_FILTER = "stale"


def stale_cutoff_expr(stale_after_seconds: int) -> dict[str, object]:
    """
    The staleness cutoff as an aggregation expression Mongo evaluates on its own clock.

    `$$NOW`, not a rendered timestamp: the keyset cursor is HMAC-bound to
    the filter document, so it must not change per request (ADR-0029 update).

    Args:
        stale_after_seconds (int): `INVENTORY_STALE_AFTER_SECONDS`.

    Returns:
        dict[str, object]: An ISO-8601 string expression comparable with the
            stored `last_seen_at` strings (ADR-0006); a missing field sorts
            below any string, so a never-seen server counts as stale.
    """
    return {
        "$dateToString": {"date": {"$subtract": ["$$NOW", stale_after_seconds * 1000]}},
    }


def build_filter_query(
    filters: dict[str, object], *, stale_after_seconds: int | None = None
) -> dict[str, object]:
    """
    Translate whitelisted filter query-param names to real Mongo field paths.

    Args:
        filters (dict[str, object]): Query-param filter key/value pairs.
        stale_after_seconds (int | None): Needed only when `filters` carries
            `stale`; the window a server must be unseen for to count.

    Returns:
        dict[str, object]: The same values, keyed by their Mongo field path.

    Raises:
        UnknownFilterError: If a key is outside `FILTER_FIELDS`, or `stale`
            is given without `stale_after_seconds`.
    """
    query: dict[str, object] = {}
    for key, value in filters.items():
        if key == STALE_FILTER:
            if stale_after_seconds is None:
                raise UnknownFilterError(
                    "Unknown filter: 'stale'",
                    details={"filter": key, "allowed": sorted(FILTER_FIELDS)},
                )
            op = "$lt" if value else "$gte"
            query["$expr"] = {op: ["$last_seen_at", stale_cutoff_expr(stale_after_seconds)]}
            continue
        if key not in FILTER_FIELDS:
            raise UnknownFilterError(
                f"Unknown filter: {key!r}",
                details={"filter": key, "allowed": sorted([*FILTER_FIELDS, STALE_FILTER])},
            )
        # Absence of a site is stored as null, so it has no spelling to
        # match on: `?site_id=unassigned` names the state instead.
        if key == "site_id" and value == UNASSIGNED_SITE_ID:
            query[FILTER_FIELDS[key]] = None
        else:
            query[FILTER_FIELDS[key]] = value
    return query


def resolve_sort_field(sort: str) -> str:
    """
    Translate a whitelisted sort query-param name to its real Mongo field path.

    Args:
        sort (str): The API sort query-param name.

    Returns:
        str: The corresponding Mongo field path.

    Raises:
        UnknownSortFieldError: If `sort` is outside `SORT_FIELDS`.
    """
    if sort not in SORT_FIELDS:
        raise UnknownSortFieldError(
            f"Unknown sort field: {sort!r}",
            details={"sort": sort, "allowed": sorted(SORT_FIELDS)},
        )
    return SORT_FIELDS[sort]


def build_search_query(raw_query: str) -> dict[str, object]:
    """
    Validate a raw search string's length and build its safe Mongo filter fragment.

    The single place server search escapes user input for `$regex`.

    Args:
        raw_query (str): The raw search string from the API request.

    Returns:
        dict[str, object]: A `search_tokens` filter fragment matching an
            escaped, anchored prefix.

    Raises:
        SearchQueryTooShortError: If `raw_query` is shorter than `MIN_SEARCH_QUERY_LENGTH`.
        SearchQueryTooLongError: If `raw_query` is longer than `MAX_SEARCH_QUERY_LENGTH`.
    """
    if len(raw_query) < MIN_SEARCH_QUERY_LENGTH:
        raise SearchQueryTooShortError(
            f"Search query must be at least {MIN_SEARCH_QUERY_LENGTH} characters.",
            details={"min_length": MIN_SEARCH_QUERY_LENGTH},
        )
    if len(raw_query) > MAX_SEARCH_QUERY_LENGTH:
        raise SearchQueryTooLongError(
            f"Search query must be at most {MAX_SEARCH_QUERY_LENGTH} characters.",
            details={"max_length": MAX_SEARCH_QUERY_LENGTH},
        )
    lowered = raw_query.lower()
    return {"search_tokens": {"$regex": "^" + re.escape(lowered)}}
