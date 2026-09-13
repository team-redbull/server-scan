"""FastAPI dependency providers.

Resource *construction* (the Mongo/Redis clients) happens once in
`app.main`'s lifespan and is stashed on `app.state`; these dependency
functions only *retrieve* what's already there. This keeps route handlers
free of any global-singleton imports, which is what makes
`app.dependency_overrides` usable in tests without monkeypatching module
globals.

Every provider here is `async def`, not `def`, even though none of them
await anything — a sync dependency is still dispatched through Starlette's
`run_in_threadpool`, and an isolated A/B measured that handoff at
+0.14 ms per dependency (`docs/notes/2026-09-research-performance.md`
§7.4). `list_servers` alone pulls in three of these, so this was the
largest single measured cost in the read path with a one-keyword fix.
"""

from __future__ import annotations

from fastapi import Request

from app.domain.models.audit_event import Actor, ActorType
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.redis import RedisClientHolder


async def get_mongo_holder(request: Request) -> MongoClientHolder:
    """
    The process-wide `MongoClientHolder` the lifespan already connected.

    Args:
        request (Request): The current request.

    Returns:
        MongoClientHolder: The connected holder stashed on `app.state`.
    """
    holder: MongoClientHolder = request.app.state.mongo
    return holder


async def get_redis_holder(request: Request) -> RedisClientHolder:
    """
    The process-wide `RedisClientHolder` the lifespan already connected.

    Args:
        request (Request): The current request.

    Returns:
        RedisClientHolder: The connected holder stashed on `app.state`.
    """
    holder: RedisClientHolder = request.app.state.redis
    return holder


async def get_request_id(request: Request) -> str | None:
    """
    This request's id, bound by `RequestContextMiddleware`.

    Args:
        request (Request): The current request.

    Returns:
        str | None: The request id, or None if the middleware hasn't run.
    """
    return getattr(request.state, "request_id", None)


# Stands in until authentication lands (CLAUDE.md, convention 6).
_UNAUTHENTICATED_ACTOR = Actor(type=ActorType.USER, id="unauthenticated", display="API (no auth)")


async def get_current_actor(_request: Request) -> Actor:
    """
    The actor an audit event should be attributed to.

    Always the well-known unauthenticated actor today — see the module
    comment above for why.

    Args:
        _request (Request): Unused; kept so this matches every other
            dependency's signature and swaps in cleanly once auth lands.

    Returns:
        Actor: `_UNAUTHENTICATED_ACTOR`.
    """
    return _UNAUTHENTICATED_ACTOR
