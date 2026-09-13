"""Cache-aside `CacheClient`, wrapping the Redis connection pool.

Every method here catches every failure mode Redis can produce
(`redis.exceptions.RedisError` and its subclasses, plus a socket-level
`TimeoutError`) and degrades to a no-op instead of raising: `get()` returns
`None` (indistinguishable from a cache miss to the caller, which is
exactly the point — the caller falls through to MongoDB either way) and
`set()`/`delete()` silently do nothing. This is what makes the "degrade to
Mongo on Redis failure" contract described in
`app.infrastructure.redis.client` actually hold at every call site,
without every route handler needing its own try/except around a cache
call.

JSON (not msgpack) is deliberate for Phase 1: msgpack isn't a project
dependency yet and adding one for a serialization format that's purely an
internal implementation detail isn't worth it until there's a measured
reason to.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from redis.exceptions import RedisError

from app.infrastructure.redis.client import RedisClientHolder
from app.observability.metrics import cache_operations_total

logger = structlog.get_logger(__name__)

# Per resource shape, not `cache_default_ttl_seconds` (docs/architecture.md, "caching").
SERVER_DETAIL_TTL_SECONDS = 60
FACETS_TTL_SECONDS = 60
LIST_PAGE_TTL_SECONDS = 15

# redis-py raises the stdlib `TimeoutError`, not a `RedisError`, on socket timeouts.
_CACHE_EXCEPTIONS = (RedisError, TimeoutError)


class CacheClient:
    """
    Cache-aside wrapper. Never raises.

    Every method degrades to a no-op/`None` on any Redis failure so
    callers never need their own try/except around a cache call.
    """

    def __init__(self, redis: RedisClientHolder) -> None:
        """
        Store the shared Redis client holder.

        Args:
            redis (RedisClientHolder): The connected client holder.
        """
        self._redis = redis

    async def get(self, key: str) -> Any | None:
        """
        Read and JSON-decode a cached value.

        Any Redis failure or malformed payload is `None`, indistinguishable
        from a miss, so the caller falls through to MongoDB either way.

        Args:
            key (str): The cache key.

        Returns:
            Any | None: The decoded value, or None on a miss or failure.
        """
        try:
            raw = await self._redis.client.get(key)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.get_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="get", outcome="error").inc()
            return None

        if raw is None:
            cache_operations_total.labels(operation="get", outcome="miss").inc()
            return None

        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            # A malformed payload is a miss, never data.
            logger.warning("cache.decode_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="get", outcome="error").inc()
            return None

        cache_operations_total.labels(operation="get", outcome="hit").inc()
        return value

    async def get_raw(self, key: str) -> bytes | str | None:
        """
        Read a cached value without JSON-decoding it.

        Only for bytes handed straight back as a response body; a value
        the caller inspects goes through `get` (docs/architecture.md, "caching").

        Args:
            key (str): The cache key.

        Returns:
            bytes | str | None: The raw stored payload, or None on a miss
                or failure.
        """
        try:
            raw = await self._redis.client.get(key)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.get_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="get", outcome="error").inc()
            return None

        if raw is None:
            cache_operations_total.labels(operation="get", outcome="miss").inc()
            return None

        cache_operations_total.labels(operation="get", outcome="hit").inc()
        return raw

    async def set(self, key: str, value: object, *, ttl_seconds: int) -> None:
        """
        JSON-encode and store a value with a TTL.

        Degrades to a no-op, logged, on a non-serializable value or any
        Redis failure — never raises.

        Args:
            key (str): The cache key.
            value (object): The value to store; must be JSON-serializable.
            ttl_seconds (int): Seconds until the key expires.
        """
        try:
            payload = json.dumps(value, default=str)
        except TypeError as exc:
            # A non-serializable value is a programmer error, still never raised.
            logger.warning("cache.encode_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="set", outcome="error").inc()
            return

        try:
            await self._redis.client.set(key, payload, ex=ttl_seconds)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.set_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="set", outcome="error").inc()
            return

        cache_operations_total.labels(operation="set", outcome="success").inc()

    async def delete_matching(self, *patterns: str) -> int:
        """
        Delete every key matching any glob. Degrades to 0 on Redis failure.

        `scan_iter`, never `KEYS`, and never on the ingest path — ADR-0028.

        Args:
            patterns (str): `SCAN MATCH` globs.

        Returns:
            int: How many keys were deleted.
        """
        deleted = 0
        try:
            for pattern in patterns:
                async for key in self._redis.client.scan_iter(match=pattern, count=500):
                    deleted += await self._redis.client.delete(key)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.delete_matching_failed", patterns=patterns, error=str(exc))
            cache_operations_total.labels(operation="delete_matching", outcome="error").inc()
            return deleted

        cache_operations_total.labels(operation="delete_matching", outcome="success").inc()
        return deleted

    async def delete(self, key: str) -> None:
        """
        Delete a cached key. Degrades to a no-op on any Redis failure.

        Args:
            key (str): The cache key to delete.
        """
        try:
            await self._redis.client.delete(key)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.delete_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="delete", outcome="error").inc()
            return

        cache_operations_total.labels(operation="delete", outcome="success").inc()
