"""Signed, stateless login sessions (docs/adr/0034).

The same HMAC-SHA256-over-base64url scheme as `app.domain.services.cursor`,
with its own secret (`Settings.session_secret`) — a stolen cursor and a
stolen session cookie must not be interchangeable. No JWT library: the
payload is closed (three fields, this codebase's own claim names), so a
generic multi-algorithm token format buys nothing a `hmac.compare_digest`
check doesn't already give.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.models.audit_event import Role

# Shared by `app.api.v1.auth` (sets/reads it) and `app.dependencies`
# (reads it) so the name lives in exactly one place.
SESSION_COOKIE_NAME = "server_scan_session"


@dataclass(frozen=True, slots=True)
class SessionClaims:
    """A verified session's contents."""

    username: str
    role: Role
    expires_at: datetime


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def encode_session(*, username: str, role: Role, secret: str, ttl_seconds: int) -> str:
    """
    Sign a session token for `username`/`role`, valid for `ttl_seconds`.

    Args:
        username (str): The authenticated user's `sAMAccountName`.
        role (Role): The role resolved at login.
        secret (str): The HMAC key (`Settings.session_secret`).
        ttl_seconds (int): Seconds from now until the session expires.

    Returns:
        str: An opaque `"<payload_b64>.<signature_b64>"` token.
    """
    expires_at = datetime.now(UTC).timestamp() + ttl_seconds
    payload = {"sub": username, "role": role.value, "exp": expires_at}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    signature = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_b64}.{_b64encode(signature)}"


def decode_session(token: str, *, secret: str) -> SessionClaims | None:
    """
    Verify and decode a session token produced by `encode_session`.

    Never raises — a missing, tampered or expired token is all just "not
    logged in" to `app.dependencies.get_current_actor`.

    Args:
        token (str): The cookie value.
        secret (str): The HMAC key the token should have been signed with.

    Returns:
        SessionClaims | None: The verified claims, or None if the token is
            malformed, unsigned by `secret`, or expired.
    """
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        expected_signature = hmac.new(
            secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        actual_signature = _b64decode(signature_b64)
    except (ValueError, TypeError):
        return None

    if not hmac.compare_digest(expected_signature, actual_signature):
        return None

    try:
        payload = json.loads(_b64decode(payload_b64))
        username = str(payload["sub"])
        role = Role(str(payload["role"]))
        expires_at = datetime.fromtimestamp(float(payload["exp"]), tz=UTC)
    except (ValueError, TypeError, KeyError):
        return None

    if expires_at <= datetime.now(UTC):
        return None

    return SessionClaims(username=username, role=role, expires_at=expires_at)
