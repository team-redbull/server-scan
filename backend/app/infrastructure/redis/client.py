"""Redis client lifecycle.

One connection pool per process, created at startup. Redis is an ephemeral
cache only (see `app.infrastructure.redis.cache`): every code path that
reads from Redis must have a MongoDB fallback, so a Redis outage degrades
request latency rather than causing request failure. That contract is
enforced here by never letting a Redis error escape `ping()`, and by the
`CacheClient` wrapper (added in the caching slice) catching every Redis
exception at the call site.
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings

logger = structlog.get_logger(__name__)


class RedisClientHolder:
    """Owns the single Redis connection pool for this process."""

    def __init__(self, settings: Settings) -> None:
        """
        Store settings; connect() creates the actual connection pool.

        Args:
            settings (Settings): Provides the Redis URI, pool size and
                timeouts used by `connect`.
        """
        self._settings = settings
        self._client: Redis | None = None

    async def connect(self) -> None:
        """
        Create the process-wide Redis connection pool.

        Idempotent. A failed startup ping is logged, not raised: Redis is
        a cache, not a dependency the service requires to start.
        """
        if self._client is not None:
            return
        self._client = Redis.from_url(
            self._settings.redis_uri,
            socket_connect_timeout=self._settings.redis_connect_timeout_seconds,
            socket_timeout=self._settings.redis_socket_timeout_seconds,
            max_connections=self._settings.redis_max_connections,
            health_check_interval=30,
            retry_on_timeout=False,
        )
        try:
            await self._client.ping()
            logger.info("redis.connected")
        except RedisError:
            logger.warning("redis.connect_failed_at_startup")

    async def close(self) -> None:
        """Close the connection pool and release it, if connected."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("redis.closed")

    @property
    def client(self) -> Redis:
        """
        The underlying Redis client.

        Returns:
            Redis: The client handle.

        Raises:
            RuntimeError: If `connect()` has not been called yet.
        """
        if self._client is None:
            raise RuntimeError("RedisClientHolder.connect() was not called")
        return self._client

    async def ping(self) -> bool:
        """
        Check Redis connectivity. Never raises — returns False on failure.

        Returns:
            bool: True if the ping succeeded, False otherwise.
        """
        if self._client is None:
            return False
        try:
            await self._client.ping()
        except RedisError:
            return False
        else:
            return True
