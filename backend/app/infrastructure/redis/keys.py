"""Redis cache key builders.

`si:<version>:...` namespaces every key so a future incompatible cache
payload change (e.g. adding a field the old format didn't have) can bump
`_NAMESPACE_VERSION` and get a clean cache rather than serving stale/
malformed payloads to a newer app version.

`server_key` embeds the document's `revision` rather than requiring an
explicit cache-invalidation call on update: a write bumps `revision`
(`AuditFields`), which changes the key, so the old entry is simply never
read again and expires on its own TTL — no invalidation path to get wrong
or forget.
"""

from __future__ import annotations

_NAMESPACE_VERSION = 1


def server_key(server_id: str, revision: int) -> str:
    """
    The cache key for one server document at one revision.

    Args:
        server_id (str): The server's id.
        revision (int): The server's current revision.

    Returns:
        str: The key.
    """
    return f"si:{_NAMESPACE_VERSION}:srv:{server_id}:r{revision}"


def list_key(filter_hash: str, cursor_hash: str) -> str:
    """
    The cache key for one page of a filtered/sorted server list.

    Args:
        filter_hash (str): A stable hash of the filters, search string,
            sort field and direction.
        cursor_hash (str): A stable hash of the pagination cursor.

    Returns:
        str: The key.
    """
    return f"si:{_NAMESPACE_VERSION}:list:{filter_hash}:{cursor_hash}"


def list_and_facets_patterns() -> tuple[str, ...]:
    """
    The globs matching every cached list page and facet count.

    Two patterns, because Redis `SCAN MATCH` has no brace alternation — a
    `{list,facets}` glob matches the literal string and so nothing at all.

    Returns:
        tuple[str, ...]: `SCAN MATCH` patterns.
    """
    return (
        f"si:{_NAMESPACE_VERSION}:list:*",
        f"si:{_NAMESPACE_VERSION}:facets:*",
        rows_key(),
    )


def rows_key() -> str:
    """
    The one key holding the whole-fleet rows body (`GET /servers/rows`).

    Returns:
        str: The cache key.
    """
    return f"si:{_NAMESPACE_VERSION}:rows"


def facets_key(filter_hash: str) -> str:
    """
    The cache key for one filtered view's facet counts.

    No cursor component, unlike `list_key`: the counts describe the whole
    filtered set, so every page of the same query shares one entry.

    Args:
        filter_hash (str): A stable hash of the filters and search string.

    Returns:
        str: The key.
    """
    return f"si:{_NAMESPACE_VERSION}:facets:{filter_hash}"
