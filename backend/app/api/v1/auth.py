"""`POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`.

The only router that stays reachable with no session — `app.main` mounts
every other router with `Depends(get_current_actor)`, which is exactly
what `/auth/login` exists to satisfy. See docs/adr/0034.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response

from app.api.v1.auth_schemas import LoginRequest, LoginResponse, MeResponse
from app.application.services.auth_service import AuthService
from app.application.services.group_membership_cache import CachedGroupMembership
from app.config import Settings, get_settings
from app.dependencies import get_redis_holder
from app.domain.models.audit_event import Role
from app.domain.services.authz import LoginResult
from app.domain.services.session import SESSION_COOKIE_NAME, decode_session, encode_session
from app.errors import ForbiddenError, RateLimitedError, ServiceUnavailableError, UnauthorizedError
from app.infrastructure.ad.client import AdApiClient, build_ad_api_http_client
from app.infrastructure.redis.cache import CacheClient
from app.infrastructure.redis.client import RedisClientHolder
from app.infrastructure.redis.login_throttle import LoginThrottle
from app.observability.metrics import auth_logins_total

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


async def _auth_service(
    settings: Annotated[Settings, Depends(get_settings)],
    redis: Annotated[RedisClientHolder, Depends(get_redis_holder)],
) -> AsyncIterator[AuthService]:
    """
    Build the auth service for one request, closing its AD API connection after.

    Args:
        settings (Settings): Supplies the LDAP/AD API config and the four
            admin/view lists.
        redis (RedisClientHolder): Backs the group-membership cache.

    Yields:
        AuthService: A service bound to a fresh `AdApiClient` behind the Redis cache.
    """
    async with build_ad_api_http_client(settings) as http:
        ad_api = AdApiClient(
            http,
            client_id=settings.ad_api_client_id.get_secret_value(),
            retries=settings.ad_api_retries,
        )
        cached = CachedGroupMembership(
            ad_api, CacheClient(redis), ttl_seconds=settings.auth_group_cache_ttl_seconds
        )
        yield AuthService(settings, cached)


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    service: Annotated[AuthService, Depends(_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: Annotated[RedisClientHolder, Depends(get_redis_holder)],
) -> LoginResponse:
    """
    Authenticate against AD and, on success, set the session cookie.

    Args:
        payload (LoginRequest): The username/password to authenticate.
        response (Response): Carries the session cookie back on success.
        service (AuthService): Runs the LDAP bind and role resolution.
        settings (Settings): Supplies the session secret, TTL and lockout limits.
        redis (RedisClientHolder): Backs the per-username failure counter.

    Returns:
        LoginResponse: The authenticated username and resolved role.

    Raises:
        RateLimitedError: Too many recent failures for this username (429).
        UnauthorizedError: Wrong username or password.
        ForbiddenError: Valid credentials, but not admin/viewer listed.
        ServiceUnavailableError: LDAP or the AD API is unreachable.
    """
    started = time.monotonic()
    outcome = "error"
    with structlog.contextvars.bound_contextvars(username=payload.username.strip().lower()):
        try:
            throttle = LoginThrottle(redis, settings)
            await throttle.check(payload.username)
            result = await service.authenticate(payload.username, payload.password)
            if result is None:
                outcome = "wrong_password"
                await throttle.record_failure(payload.username)
                raise UnauthorizedError("Wrong username or password.")
            await throttle.reset(payload.username)
            if result is LoginResult.NO_PERMISSION:
                outcome = "no_permission"
                raise ForbiddenError("Your account has no permission for this app.")
            outcome = "success"
        except RateLimitedError:
            outcome = "throttled"
            raise
        except ServiceUnavailableError:
            outcome = "unavailable"
            raise
        finally:
            auth_logins_total.labels(outcome=outcome).inc()
            log = logger.warning if outcome in {"unavailable", "error"} else logger.info
            log(
                "auth.login",
                outcome=outcome,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )

    username = payload.username.strip().lower()
    token = encode_session(
        username=username,
        role=result,
        secret=settings.session_secret,
        ttl_seconds=settings.session_ttl_seconds,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=settings.environment == "production",
        path="/",
    )
    return LoginResponse(username=username, role=result)


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    """
    Clear the session cookie.

    Args:
        response (Response): Carries the cookie deletion back.
    """
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> MeResponse:
    """
    Report whether login is required and, if so, who's logged in.

    Args:
        request (Request): Carries the session cookie, if any.
        settings (Settings): Supplies `auth_enabled` and the session secret.

    Returns:
        MeResponse: `login_required=False` (and an auto-admin identity)
            when auth is disabled; otherwise the verified cookie's identity,
            or `authenticated=False` if there isn't one.
    """
    if not settings.auth_enabled:
        return MeResponse(login_required=False, authenticated=True, username="dev", role=Role.ADMIN)

    token = request.cookies.get(SESSION_COOKIE_NAME)
    claims = decode_session(token, secret=settings.session_secret) if token else None
    if claims is None:
        return MeResponse(login_required=True, authenticated=False)
    return MeResponse(
        login_required=True, authenticated=True, username=claims.username, role=claims.role
    )
