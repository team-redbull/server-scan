"""Redis cache in front of the AD API's group-membership lookup (docs/adr/0042).

Keyed per group, not per user, so every login that checks the same admin or
viewer group shares one AD API call. Redis is a cache here: any failure falls
through to the AD API, and a failed AD API call is never cached or papered
over with a stale answer (a demoted admin must not keep access on an outage).
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog

from app.application.services.auth_service import GroupMembershipLookup
from app.infrastructure.redis.keys import group_members_key

logger = structlog.get_logger(__name__)


class _Cache(Protocol):
    """The two `CacheClient` methods used here, so a test can fake them."""

    async def get(self, key: str) -> Any | None:
        """Read a cached value, None on a miss or failure."""
        ...

    async def set(self, key: str, value: object, *, ttl_seconds: int) -> None:
        """Store a value with a TTL, never raising."""
        ...


class CachedGroupMembership:
    """`GroupMembershipLookup` that answers from Redis for `ttl_seconds` after each AD API call."""

    def __init__(self, inner: GroupMembershipLookup, cache: _Cache, *, ttl_seconds: int) -> None:
        """
        Build the wrapper.

        Args:
            inner (GroupMembershipLookup): The real lookup (the AD API client).
            cache (_Cache): A `CacheClient`, which never raises, so a Redis outage is just a miss.
            ttl_seconds (int): How long a member list stays fresh; 0 bypasses the cache.
        """
        self._inner = inner
        self._cache = cache
        self._ttl = ttl_seconds

    async def group_members(self, group_sam: str) -> set[str]:
        """
        Return the lower-cased members of `group_sam`, from Redis if fresh.

        Args:
            group_sam (str): The group's `sAMAccountName`.

        Returns:
            set[str]: Lower-cased member usernames.

        Raises:
            ServiceUnavailableError: The cache missed and the AD API failed.
        """
        if self._ttl == 0:
            return await self._inner.group_members(group_sam)
        key = group_members_key(group_sam.strip().lower())
        cached = await self._cache.get(key)
        if isinstance(cached, list):
            logger.info("auth.group_cache", group=group_sam, outcome="hit", members=len(cached))
            return {str(member) for member in cached}
        logger.info("auth.group_cache", group=group_sam, outcome="miss")
        members = await self._inner.group_members(group_sam)
        await self._cache.set(key, sorted(members), ttl_seconds=self._ttl)
        return members
