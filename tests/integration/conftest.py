"""Shared fixtures for tests against the live dev Mongo+Redis stack
(`scripts/dev-up.sh`). Every fixture here skips cleanly — never hangs —
when its backing service isn't reachable, so `pytest tests/integration`
degrades to a clear skip list instead of a timeout when the dev stack
isn't running.

The reachability check itself (the memoized "unreachable" reason,
remembered for the rest of the session so ~60 function-scoped, Mongo-backed
tests don't each pay `mongo_server_selection_timeout_ms` (5s) to rediscover
the same dead server) lives in `tests._stack_availability`, shared with
`tests/api/conftest.py` rather than duplicated — see that module's
docstring for the measured before/after (one file of 7 skips went from 35s
to part of a 5.22s run).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from tests._stack_availability import connect_mongo_or_skip, connect_redis_or_skip

from app.config import get_settings
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.redis import RedisClientHolder

_TEST_COLLECTIONS = (
    "servers",
    "sites",
    "managers",
    "membership_runs",
    "classification_rules",
    "health_policies",
    "audit_events",
)


@pytest.fixture
async def mongo_holder() -> AsyncIterator[MongoClientHolder]:
    holder = await connect_mongo_or_skip(get_settings())

    await ensure_indexes(holder.db)
    for name in _TEST_COLLECTIONS:
        await holder.db[name].delete_many({})

    try:
        yield holder
    finally:
        for name in _TEST_COLLECTIONS:
            await holder.db[name].delete_many({})
        await holder.close()


@pytest.fixture
async def redis_holder() -> AsyncIterator[RedisClientHolder]:
    holder = await connect_redis_or_skip(get_settings())
    try:
        yield holder
    finally:
        await holder.close()
