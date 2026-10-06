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

import hashlib

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


def list_cache_patterns() -> tuple[str, ...]:
    """
    The globs matching every cached list page and the whole-fleet rows body.

    Returns:
        tuple[str, ...]: `SCAN MATCH` patterns.
    """
    return (f"si:{_NAMESPACE_VERSION}:list:*", rows_key())


def server_cache_patterns(server_id: str) -> tuple[str, ...]:
    """
    The globs matching one server's cached detail payloads and revision pointer.

    Args:
        server_id (str): The server's id.

    Returns:
        tuple[str, ...]: `SCAN MATCH` patterns.
    """
    return (f"si:{_NAMESPACE_VERSION}:srv:{server_id}:*",)


def rows_key() -> str:
    """
    The one key holding the whole-fleet rows body (`GET /servers/rows`).

    Returns:
        str: The cache key.
    """
    return f"si:{_NAMESPACE_VERSION}:rows"


def login_failure_key(username: str) -> str:
    """
    The counter key for one username's failed logins.

    The username is hashed so the key is bounded and free of arbitrary bytes.

    Args:
        username (str): The submitted username, already normalized.

    Returns:
        str: The key.
    """
    digest = hashlib.sha256(username.encode()).hexdigest()[:32]
    return f"si:{_NAMESPACE_VERSION}:loginfail:{digest}"


def group_members_key(group_sam: str) -> str:
    """
    The cache key for one AD group's recursive member list.

    Args:
        group_sam (str): The group's `sAMAccountName`, already lower-cased.

    Returns:
        str: The key.
    """
    digest = hashlib.sha256(group_sam.encode()).hexdigest()[:32]
    return f"si:{_NAMESPACE_VERSION}:adgroup:{digest}"
