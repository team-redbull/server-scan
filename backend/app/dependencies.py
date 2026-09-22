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

import hmac
from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.domain.models.audit_event import Actor, ActorType, Role
from app.domain.services.session import SESSION_COOKIE_NAME, decode_session
from app.errors import ForbiddenError, UnauthorizedError
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


# `Settings.auth_enabled=False` (this repo's own dev/test default — no AD
# reachable here): every caller is this fixed dev admin. See docs/adr/0034.
_DEV_ACTOR = Actor(type=ActorType.USER, id="dev", display="Dev (auth disabled)", role=Role.ADMIN)


async def get_current_actor(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> Actor:
    """
    Resolve the caller: the dev bypass, a bearer API token, or a session cookie, in that order.

    See docs/adr/0034 for where this is mounted and how `require_admin` layers on it.

    Args:
        request (Request): Carries the `Authorization` header and the
            session cookie.
        settings (Settings): Supplies `auth_enabled`, the two API tokens,
            and the session secret.

    Returns:
        Actor: The resolved caller, with its `role` set.

    Raises:
        UnauthorizedError: `auth_enabled` is true and neither a valid
            token nor a valid session cookie was presented.
    """
    if not settings.auth_enabled:
        return _DEV_ACTOR

    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        admin_token = settings.api_token_admin.get_secret_value()
        if admin_token and hmac.compare_digest(token, admin_token):
            return Actor(type=ActorType.TOKEN, id="api-token-admin", role=Role.ADMIN)
        viewer_token = settings.api_token_viewer.get_secret_value()
        if viewer_token and hmac.compare_digest(token, viewer_token):
            return Actor(type=ActorType.TOKEN, id="api-token-viewer", role=Role.VIEWER)

    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    claims = decode_session(cookie, secret=settings.session_secret) if cookie else None
    if claims is not None:
        return Actor(type=ActorType.USER, id=claims.username, role=claims.role)

    raise UnauthorizedError("Login required.")


async def require_admin(actor: Annotated[Actor, Depends(get_current_actor)]) -> Actor:
    """
    Require the resolved caller to be an admin.

    Args:
        actor (Actor): The already-authenticated caller.

    Returns:
        Actor: `actor`, unchanged.

    Raises:
        ForbiddenError: `actor.role` is not `Role.ADMIN`.
    """
    if actor.role is not Role.ADMIN:
        raise ForbiddenError("Only admins can do this.")
    return actor
