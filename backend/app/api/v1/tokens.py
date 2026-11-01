"""`/api/v1/auth/token` — a signed-in user's one personal API token (ADR-0053)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.v1.token_schemas import TokenCreated, TokenInfo
from app.application.services.audit_service import AuditService
from app.application.services.personal_token import PersonalTokenService
from app.config import Settings, get_settings
from app.dependencies import get_current_actor, get_mongo_holder, require_admin
from app.domain.models.api_token import ApiToken
from app.domain.models.audit_event import Actor, ActorType
from app.errors import ForbiddenError, NotFoundError
from app.infrastructure.mongodb.api_token_repository import MongoApiTokenRepository
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.client import MongoClientHolder

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _info(token: ApiToken) -> TokenInfo:
    """
    Describe a stored token without its secret.

    Args:
        token (ApiToken): The stored record.

    Returns:
        TokenInfo: Its owner, role, prefix and dates.
    """
    return TokenInfo(
        username=token.username,
        role=token.role,
        prefix=token.prefix,
        created_at=token.created_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
    )


async def _repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoApiTokenRepository:
    """
    Build the token store for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.

    Returns:
        MongoApiTokenRepository: A store bound to that client.
    """
    return MongoApiTokenRepository(mongo)


async def _service(
    repo: Annotated[MongoApiTokenRepository, Depends(_repo)],
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PersonalTokenService:
    """
    Build the token service for one request.

    Args:
        repo (MongoApiTokenRepository): The token store.
        mongo (MongoClientHolder): Backs the audit trail.
        settings (Settings): Supplies the token lifetime.

    Returns:
        PersonalTokenService: A service bound to them.
    """
    audit = AuditService(repo=MongoAuditEventRepository(mongo))
    return PersonalTokenService(repo, audit, settings.personal_token_ttl_days)


def _login_on(settings: Settings) -> None:
    """
    Refuse when login is off, since nobody then has a token.

    Args:
        settings (Settings): Says whether login is on.

    Raises:
        NotFoundError: Login is off.
    """
    if not settings.auth_enabled:
        raise NotFoundError("Login is off on this deployment; no token is needed.")


def _signed_in_user(actor: Actor, settings: Settings) -> Actor:
    """
    Accept only a person signed in with a session, never a token.

    Args:
        actor (Actor): The resolved caller.
        settings (Settings): Says whether login is on at all.

    Returns:
        Actor: The same actor.

    Raises:
        NotFoundError: Login is off, so nobody needs a token.
        ForbiddenError: The caller is a token, which cannot mint another.
    """
    _login_on(settings)
    if actor.type is not ActorType.USER:
        raise ForbiddenError("Sign in to manage a personal token; a token cannot issue one.")
    return actor


@router.get("/token", response_model=TokenInfo | None)
async def my_token(
    actor: Annotated[Actor, Depends(get_current_actor)],
    repo: Annotated[MongoApiTokenRepository, Depends(_repo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenInfo | None:
    """
    The caller's token, without its secret.

    Args:
        actor (Actor): The caller.
        repo (MongoApiTokenRepository): The token store.
        settings (Settings): Says whether login is on.

    Returns:
        TokenInfo | None: Their token, or `None` if they have not made one.
    """
    token = await repo.get(_signed_in_user(actor, settings).id)
    return _info(token) if token else None


@router.post("/token", response_model=TokenCreated, status_code=201)
async def create_my_token(
    actor: Annotated[Actor, Depends(get_current_actor)],
    service: Annotated[PersonalTokenService, Depends(_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenCreated:
    """
    Issue the caller a new token, replacing any they had.

    Args:
        actor (Actor): The caller.
        service (PersonalTokenService): Issues and records it.
        settings (Settings): Says whether login is on.

    Returns:
        TokenCreated: The token itself, shown only in this response, and its details.
    """
    token, plain = await service.create(_signed_in_user(actor, settings))
    return TokenCreated(token=plain, info=_info(token))


@router.delete("/token", status_code=204)
async def revoke_my_token(
    actor: Annotated[Actor, Depends(get_current_actor)],
    service: Annotated[PersonalTokenService, Depends(_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """
    Revoke the caller's token.

    Args:
        actor (Actor): The caller.
        service (PersonalTokenService): Removes and records it.
        settings (Settings): Says whether login is on.

    Returns:
        Response: Empty, 204, whether or not they had one.
    """
    user = _signed_in_user(actor, settings)
    await service.revoke(user, user.id)
    return Response(status_code=204)


@router.get("/tokens", response_model=list[TokenInfo])
async def all_tokens(
    _: Annotated[Actor, Depends(require_admin)],
    repo: Annotated[MongoApiTokenRepository, Depends(_repo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[TokenInfo]:
    """
    Every user's token, for an admin.

    Args:
        _ (Actor): The admin caller.
        repo (MongoApiTokenRepository): The token store.
        settings (Settings): Says whether login is on.

    Returns:
        list[TokenInfo]: One per user who has a token, newest first.

    Raises:
        NotFoundError: Login is off, so there are no tokens.
    """
    _login_on(settings)
    return [_info(t) for t in await repo.list_all()]


@router.delete("/tokens/{username}", status_code=204)
async def revoke_token_of(
    username: str,
    admin: Annotated[Actor, Depends(require_admin)],
    service: Annotated[PersonalTokenService, Depends(_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """
    Revoke another user's token, e.g. when they leave.

    Args:
        username (str): Whose token.
        admin (Actor): The admin caller.
        service (PersonalTokenService): Removes and records it.
        settings (Settings): Says whether login is on.

    Returns:
        Response: Empty, 204.

    Raises:
        NotFoundError: Login is off, or that user has no token.
    """
    _login_on(settings)
    if not await service.revoke(admin, username):
        raise NotFoundError("That user has no token.")
    return Response(status_code=204)
