"""Response shapes for the personal-token endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.models.audit_event import Role


class TokenInfo(BaseModel):
    """
    A personal token, without its secret.

    Attributes:
        username (str): The owner.
        role (Role): What it may do.
        prefix (str): Its first characters, to tell it apart.
        created_at (datetime): When it was issued.
        expires_at (datetime): When it stops working.
        last_used_at (datetime | None): When it was last used (to within minutes).
    """

    username: str
    role: Role
    prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None


class TokenCreated(BaseModel):
    """
    A freshly issued token.

    Attributes:
        token (str): The secret, returned only here; it cannot be read again.
        info (TokenInfo): Its details.
    """

    token: str
    info: TokenInfo
