"""`POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`.

The only router that stays reachable with no session — `app.main` mounts
every other router with `Depends(get_current_actor)`, which is exactly
what `/auth/login` exists to satisfy. See docs/adr/0034.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.v1.auth_schemas import LoginRequest, LoginResponse, MeResponse
from app.application.services.auth_service import AuthService
from app.config import Settings, get_settings
from app.domain.models.audit_event import Role
from app.domain.services.authz import LoginResult
from app.domain.services.session import SESSION_COOKIE_NAME, decode_session, encode_session
from app.errors import ForbiddenError, UnauthorizedError
from app.infrastructure.ad.client import AdApiClient, build_ad_api_http_client

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


async def _auth_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[AuthService]:
    """
    Build the auth service for one request, closing its AD API connection after.

    Args:
        settings (Settings): Supplies the LDAP/AD API config and the four
            admin/view lists.

    Yields:
        AuthService: A service bound to a fresh `AdApiClient`.
    """
    async with build_ad_api_http_client(settings) as http:
        ad_api = AdApiClient(http, client_id=settings.ad_api_client_id.get_secret_value())
        yield AuthService(settings, ad_api)


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    service: Annotated[AuthService, Depends(_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginResponse:
    """
    Authenticate against AD and, on success, set the session cookie.

    Args:
        payload (LoginRequest): The username/password to authenticate.
        response (Response): Carries the session cookie back on success.
        service (AuthService): Runs the LDAP bind and role resolution.
        settings (Settings): Supplies the session secret and TTL.

    Returns:
        LoginResponse: The authenticated username and resolved role.

    Raises:
        UnauthorizedError: Wrong username or password.
        ForbiddenError: Valid credentials, but not admin/viewer listed.
        ServiceUnavailableError: LDAP or the AD API is unreachable.
    """
    result = await service.authenticate(payload.username, payload.password)
    if result is None:
        raise UnauthorizedError("Wrong username or password.")
    if result is LoginResult.NO_PERMISSION:
        raise ForbiddenError("Your account has no permission for this app.")

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
