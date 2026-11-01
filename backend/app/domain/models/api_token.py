"""A user's personal API token (ADR-0053): the hash of it, whose it is and when it lapses."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.models.audit_event import Role


class ApiToken(BaseModel):
    """
    One stored personal token; a user has at most one.

    Attributes:
        username (str): The owner, also the document id.
        role (Role): What the token may do: `VIEWER` or `AUDITOR`, never `ADMIN`.
        token_hash (str): SHA-256 of the token; the token itself is never stored.
        prefix (str): The token's first characters, for telling tokens apart in a list.
        created_at (datetime): When it was issued.
        expires_at (datetime): When it stops working.
        last_used_at (datetime | None): When a request last used it (coarse).
    """

    username: str = Field(alias="_id")
    role: Role
    token_hash: str
    prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None

    model_config = {"populate_by_name": True}
