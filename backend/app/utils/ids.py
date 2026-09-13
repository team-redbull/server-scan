"""Application-generated document identifiers.

We never use MongoDB's ObjectId as the public `_id`: prefixed, opaque ids
(`srv_...`, `site_...`) are self-describing in logs and URLs, and keep the
domain layer free of any MongoDB-specific type. Uniqueness comes from
`uuid4`; ordering (when needed) comes from `created_at`, not from the id
itself, so we deliberately avoid taking on a ULID dependency for Phase 1.
"""

from __future__ import annotations

import uuid

ID_PREFIXES = {
    "server": "srv",
    "site": "site",
    "manager": "mgr",
    "classification_rule": "crul",
    "health_policy": "hpol",
    "event": "evt",
}


def new_id(kind: str) -> str:
    """Generate a new prefixed id for the given document kind.

    Args:
        kind (str): A key of `ID_PREFIXES`.

    Returns:
        str: `<prefix>_<uuid4 hex>`.

    Raises:
        KeyError: For an unregistered kind, rather than an unprefixed id.
    """
    prefix = ID_PREFIXES[kind]
    return f"{prefix}_{uuid.uuid4().hex}"
