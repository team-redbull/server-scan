"""Request/response schemas for `app.api.v1.auth`."""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.models.audit_event import Role


class LoginRequest(BaseModel):
    """The request body for `POST /api/v1/auth/login`."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """The response body for a successful `POST /api/v1/auth/login`."""

    username: str
    role: Role


class MeResponse(BaseModel):
    """The response body for `GET /api/v1/auth/me` — see docs/adr/0034 for the SPA's use of it."""

    login_required: bool
    authenticated: bool
    username: str | None = None
    role: Role | None = None
