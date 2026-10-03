"""An in-memory stand-in for the slice of `redis.asyncio.Redis` the login throttle uses."""

from __future__ import annotations

from typing import Any


class FakeRedis:
    """Dict-backed `get`/`ttl`/`incr`/`expire(nx=)`/`delete`; setting `fail` makes calls raise."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail: Exception | None = None

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def delete(self, key: str) -> int:
        self._check()
        self.ttls.pop(key, None)
        return int(self.values.pop(key, None) is not None)

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> _Pipeline:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def get(self, key: str) -> _Pipeline:
        self._ops.append(("get", (key,), {}))
        return self

    def ttl(self, key: str) -> _Pipeline:
        self._ops.append(("ttl", (key,), {}))
        return self

    def incr(self, key: str) -> _Pipeline:
        self._ops.append(("incr", (key,), {}))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> _Pipeline:
        self._ops.append(("expire", (key, seconds), {"nx": nx}))
        return self

    async def execute(self) -> list[Any]:
        self._redis._check()
        r, out = self._redis, []
        for name, args, kw in self._ops:
            key = args[0]
            if name == "get":
                out.append(r.values.get(key))
            elif name == "ttl":
                out.append(r.ttls.get(key, -2 if key not in r.values else -1))
            elif name == "incr":
                r.values[key] = r.values.get(key, 0) + 1
                out.append(r.values[key])
            else:
                if kw["nx"] and key in r.ttls:
                    out.append(0)
                else:
                    r.ttls[key] = args[1]
                    out.append(1)
        return out


class FakeRedisHolder:
    """Quacks like `RedisClientHolder`."""

    def __init__(self, client: FakeRedis | None = None) -> None:
        self.client = client or FakeRedis()
