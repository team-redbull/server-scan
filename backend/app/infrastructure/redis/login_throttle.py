"""Per-username failed-login counter for `POST /auth/login` (docs/adr/0039).

A failed LDAP bind counts against the real AD account, so this stops guessing
before AD's own lockout would lock the user out of everything else. Redis is
non-critical here: any Redis error lets the login through, and AD's lockout
remains the backstop.
"""

from __future__ import annotations

import structlog

from app.config import Settings
from app.errors import RateLimitedError
from app.infrastructure.redis.cache import _CACHE_EXCEPTIONS
from app.infrastructure.redis.client import RedisClientHolder
from app.infrastructure.redis.keys import login_failure_key

logger = structlog.get_logger(__name__)


class LoginThrottle:
    """Counts failed logins per username and refuses a locked one before any LDAP bind."""

    def __init__(self, redis: RedisClientHolder, settings: Settings) -> None:
        """
        Build the throttle.

        Args:
            redis (RedisClientHolder): The shared Redis client holder.
            settings (Settings): Supplies `login_max_failures` and `login_lockout_seconds`.
        """
        self._redis = redis
        self._max = settings.login_max_failures
        self._window = settings.login_lockout_seconds

    @staticmethod
    def _key(username: str) -> str:
        return login_failure_key(username.strip().lower())

    async def check(self, username: str) -> None:
        """
        Refuse the attempt if the username is locked.

        Args:
            username (str): The submitted username.

        Raises:
            RateLimitedError: The username has reached the failure limit.
        """
        if self._max == 0:
            return
        try:
            client = self._redis.client
            async with client.pipeline(transaction=False) as pipe:
                pipe.get(self._key(username))
                pipe.ttl(self._key(username))
                count, ttl = await pipe.execute()
        except (*_CACHE_EXCEPTIONS, RuntimeError):
            logger.warning("login_throttle.redis_unavailable", op="check")
            return
        if count is not None and int(count) >= self._max:
            logger.warning("login_throttle.locked_out")
            raise RateLimitedError(
                "Too many failed login attempts. Try again later.",
                retry_after_seconds=max(int(ttl), 1),
            )

    async def record_failure(self, username: str) -> None:
        """
        Count one failed login; the first failure starts the lockout window.

        Args:
            username (str): The submitted username.
        """
        if self._max == 0:
            return
        key = self._key(username)
        try:
            async with self._redis.client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window, nx=True)
                await pipe.execute()
        except (*_CACHE_EXCEPTIONS, RuntimeError):
            logger.warning("login_throttle.redis_unavailable", op="record_failure")

    async def reset(self, username: str) -> None:
        """
        Clear the counter after a login with valid credentials.

        Args:
            username (str): The submitted username.
        """
        if self._max == 0:
            return
        try:
            await self._redis.client.delete(self._key(username))
        except (*_CACHE_EXCEPTIONS, RuntimeError):
            logger.warning("login_throttle.redis_unavailable", op="reset")
