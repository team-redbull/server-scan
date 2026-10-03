"""LoginThrottle: per-username failure counting, lockout, reset, fail-open (docs/adr/0039)."""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from tests.fake_redis import FakeRedis, FakeRedisHolder

from app.config import get_settings
from app.errors import RateLimitedError
from app.infrastructure.redis.login_throttle import LoginThrottle


def _throttle(redis: FakeRedis, *, max_failures: int = 3, window: int = 600) -> LoginThrottle:
    settings = get_settings().model_copy(
        update={"login_max_failures": max_failures, "login_lockout_seconds": window}
    )
    return LoginThrottle(FakeRedisHolder(redis), settings)  # ty: ignore[invalid-argument-type]


class TestLockout:
    async def test_locks_after_max_failures_with_retry_after(self) -> None:
        throttle = _throttle(FakeRedis())
        for _ in range(3):
            await throttle.check("alice")
            await throttle.record_failure("alice")
        with pytest.raises(RateLimitedError) as err:
            await throttle.check("alice")
        assert err.value.retry_after_seconds == 600

    async def test_under_the_limit_still_allowed(self) -> None:
        throttle = _throttle(FakeRedis())
        for _ in range(2):
            await throttle.record_failure("alice")
        await throttle.check("alice")

    async def test_username_is_case_and_space_insensitive(self) -> None:
        throttle = _throttle(FakeRedis(), max_failures=1)
        await throttle.record_failure("Alice")
        with pytest.raises(RateLimitedError):
            await throttle.check("  alice ")

    async def test_users_are_counted_separately(self) -> None:
        throttle = _throttle(FakeRedis(), max_failures=1)
        await throttle.record_failure("alice")
        await throttle.check("bob")

    async def test_window_is_set_once_not_extended(self) -> None:
        redis = FakeRedis()
        throttle = _throttle(redis, window=600)
        await throttle.record_failure("alice")
        (key,) = redis.ttls
        redis.ttls[key] = 100
        await throttle.record_failure("alice")
        assert redis.ttls[key] == 100


class TestReset:
    async def test_success_clears_the_counter(self) -> None:
        throttle = _throttle(FakeRedis(), max_failures=2)
        await throttle.record_failure("alice")
        await throttle.reset("alice")
        await throttle.record_failure("alice")
        await throttle.check("alice")


class TestDisabledAndFailOpen:
    async def test_zero_disables_everything(self) -> None:
        redis = FakeRedis()
        throttle = _throttle(redis, max_failures=0)
        for _ in range(10):
            await throttle.record_failure("alice")
        await throttle.check("alice")
        assert redis.values == {}

    async def test_redis_errors_let_the_login_through(self) -> None:
        redis = FakeRedis()
        redis.fail = RedisConnectionError("down")
        throttle = _throttle(redis, max_failures=1)
        await throttle.check("alice")
        await throttle.record_failure("alice")
        await throttle.reset("alice")
