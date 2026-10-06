"""`CachedGroupMembership`: a Redis cache that never hides an AD API failure (docs/adr/0042)."""

from __future__ import annotations

import pytest

from app.application.services.group_membership_cache import CachedGroupMembership
from app.errors import ServiceUnavailableError

pytestmark = pytest.mark.unit


class _DictCache:
    """The two `CacheClient` methods the wrapper uses, backed by a dict."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.ttls: list[int] = []

    async def get(self, key: str) -> object | None:
        return self.store.get(key)

    async def set(self, key: str, value: object, *, ttl_seconds: int) -> None:
        self.store[key] = value
        self.ttls.append(ttl_seconds)


class _CountingAdApi:
    def __init__(self, members: set[str] | None = None, *, down: bool = False) -> None:
        self.members = members or {"jdoe"}
        self.down = down
        self.calls = 0

    async def group_members(self, group_sam: str) -> set[str]:
        self.calls += 1
        if self.down:
            raise ServiceUnavailableError("down", dependency="ad_api", reason="read_timeout")
        return self.members


def _wrap(inner: _CountingAdApi, cache: _DictCache, ttl: int = 120) -> CachedGroupMembership:
    return CachedGroupMembership(inner, cache, ttl_seconds=ttl)


class TestCachedGroupMembership:
    async def test_the_second_lookup_is_served_from_the_cache(self) -> None:
        inner, cache = _CountingAdApi({"a", "b"}), _DictCache()
        lookup = _wrap(inner, cache)
        assert await lookup.group_members("Admins") == {"a", "b"}
        assert await lookup.group_members("admins") == {"a", "b"}
        assert inner.calls == 1
        assert cache.ttls == [120]

    async def test_a_failed_ad_api_call_propagates_and_is_not_cached(self) -> None:
        inner, cache = _CountingAdApi(down=True), _DictCache()
        lookup = _wrap(inner, cache)
        with pytest.raises(ServiceUnavailableError):
            await lookup.group_members("Admins")
        assert cache.store == {}

    async def test_ttl_zero_bypasses_the_cache_entirely(self) -> None:
        inner, cache = _CountingAdApi(), _DictCache()
        lookup = _wrap(inner, cache, ttl=0)
        await lookup.group_members("Admins")
        await lookup.group_members("Admins")
        assert inner.calls == 2
        assert cache.store == {}

    async def test_an_empty_group_is_a_cacheable_answer(self) -> None:
        inner, cache = _CountingAdApi(set()), _DictCache()
        lookup = _wrap(inner, cache)
        await lookup.group_members("Empty")
        await lookup.group_members("Empty")
        assert inner.calls == 1
