"""Issuing, checking and revoking personal API tokens (ADR-0053)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from app.application.services.audit_service import AuditService
from app.domain.models.api_token import ApiToken
from app.domain.models.audit_event import Actor, ActorType, EventType, Role
from app.infrastructure.mongodb.api_token_repository import MongoApiTokenRepository
from app.utils.timeutil import utcnow

TOKEN_PREFIX = "ssk_"  # noqa: S105 - a public marker, not a secret
_PREFIX_SHOWN = 12
_TOUCH_EVERY = timedelta(minutes=5)


def hash_token(token: str) -> str:
    """
    Hash a presented token the way it is stored.

    Args:
        token (str): The token as the caller sent it.

    Returns:
        str: Its SHA-256 hex digest (the token is 256 random bits, so no salt or stretching).
    """
    return hashlib.sha256(token.encode()).hexdigest()


def token_role(role: Role | None) -> Role:
    """
    The role a user's personal token gets: read-only, never admin.

    Args:
        role (Role | None): The user's own role.

    Returns:
        Role: `VIEWER` for a viewer, `AUDITOR` for an auditor or an admin.
    """
    return Role.VIEWER if role is Role.VIEWER else Role.AUDITOR


def token_actor(token: ApiToken) -> Actor:
    """
    The audit identity of a request made with a personal token.

    Args:
        token (ApiToken): The token that authenticated.

    Returns:
        Actor: A token actor naming its owner, so an event reads as that user's.
    """
    return Actor(
        type=ActorType.TOKEN, id=f"token:{token.username}", display=token.username, role=token.role
    )


async def authenticate(repo: MongoApiTokenRepository, presented: str) -> Actor | None:
    """
    Resolve a presented bearer token to its owner.

    Args:
        repo (MongoApiTokenRepository): The token store.
        presented (str): The bearer value.

    Returns:
        Actor | None: The owner's token actor, or `None` for an unknown or expired token.
    """
    if not presented.startswith(TOKEN_PREFIX):
        return None
    token = await repo.find_by_hash(hash_token(presented))
    now = utcnow()
    if token is None or token.expires_at <= now:
        return None
    if token.last_used_at is None or now - token.last_used_at > _TOUCH_EVERY:
        await repo.touch(token.username, now.isoformat())
    return token_actor(token)


class PersonalTokenService:
    """A signed-in user's own token: create (replacing), look up, revoke."""

    def __init__(self, repo: MongoApiTokenRepository, audit: AuditService, ttl_days: int) -> None:
        """
        Build the service.

        Args:
            repo (MongoApiTokenRepository): The token store.
            audit (AuditService): Records creation and revocation.
            ttl_days (int): How long a new token lives.
        """
        self._repo = repo
        self._audit = audit
        self._ttl = timedelta(days=ttl_days)

    async def create(self, actor: Actor) -> tuple[ApiToken, str]:
        """
        Issue the caller a token, revoking the one they had.

        Args:
            actor (Actor): The signed-in user.

        Returns:
            tuple[ApiToken, str]: The stored record and the token itself, shown once.
        """
        plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = utcnow()
        token = ApiToken(
            _id=actor.id,
            role=token_role(actor.role),
            token_hash=hash_token(plain),
            prefix=plain[:_PREFIX_SHOWN],
            created_at=now,
            expires_at=now + self._ttl,
        )
        # Audit first: a failed write must not leave a token issued (or replaced) unrecorded.
        await self._audit.record(
            EventType.TOKEN_CREATED,
            actor=actor,
            data={
                "user": actor.id,
                "role": token.role.value,
                "expires_at": token.expires_at.isoformat(),
            },
        )
        await self._repo.replace(token)
        return token, plain

    async def revoke(self, actor: Actor, username: str) -> bool:
        """
        Remove a user's token, recorded under whoever did it.

        Args:
            actor (Actor): The caller: the owner, or an admin revoking someone's.
            username (str): Whose token.

        Returns:
            bool: Whether there was a token to remove.
        """
        if await self._repo.get(username) is None:
            return False
        await self._audit.record(
            EventType.TOKEN_REVOKED,
            actor=actor,
            data={"user": username, "by_admin": actor.id != username},
        )
        return await self._repo.delete(username)
