"""AD login orchestration: credentials, then admin-before-viewer group/user checks.

Rules (docs/adr/0034, from the operator's own AD-integration spec):

1. The LDAP bind happens first — a bad password never reaches the group checks.
2. Admin is checked first, then auditor (read-only plus the audit trail,
   docs/adr/0043), then viewer, and a user's own allow-list is checked
   before its groups, so a listed user costs no AD API call. A user in
   several lists gets the first match: the most privileged role wins.
3. Matching is case-insensitive throughout.
4. `ServiceUnavailableError` (LDAP or the AD API down) always propagates —
   it must never be reported as a wrong password.
5. The whole attempt has a deadline (`auth_login_deadline_seconds`), so
   per-call timeouts and retries can never add up to a hung login.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Protocol

import structlog

from app.config.settings import Settings
from app.domain.models.audit_event import Role
from app.domain.services.authz import LoginResult, split_csv_lower
from app.errors import ServiceUnavailableError
from app.infrastructure.ad.client import ldap_validate

logger = structlog.get_logger(__name__)


class GroupMembershipLookup(Protocol):
    """`AdApiClient`'s shape, as a Protocol so a test can fake it with no `httpx`/`Settings`."""

    async def group_members(self, group_sam: str) -> set[str]:
        """Return the lower-cased `sAMAccountName`s of every recursive member of `group_sam`."""
        ...


class AuthService:
    """Authenticates one login attempt against LDAP + the AD API."""

    def __init__(self, settings: Settings, ad_api: GroupMembershipLookup) -> None:
        """
        Build the service.

        Args:
            settings (Settings): Supplies the LDAP bind config and the four
                admin/view group and user lists.
            ad_api (GroupMembershipLookup): Queries recursive group membership.
        """
        self._settings = settings
        self._ad_api = ad_api

    async def authenticate(self, username: str, password: str) -> Role | LoginResult | None:
        """
        Authenticate one username/password and resolve its role.

        Args:
            username (str): The `sAMAccountName` to bind and look up.
            password (str): The password to bind with.

        Returns:
            Role | LoginResult | None: `Role.ADMIN`, `Role.AUDITOR` or `Role.VIEWER` if
                authenticated and permitted, `LoginResult.NO_PERMISSION` if
                authenticated but listed nowhere, or None for a wrong
                username/password.

        Raises:
            ServiceUnavailableError: LDAP or the AD API is unreachable or
                misbehaved — never conflated with a wrong password.
        """
        try:
            async with asyncio.timeout(self._settings.auth_login_deadline_seconds):
                return await self._authenticate(username, password)
        except TimeoutError as exc:
            logger.warning(
                "auth.deadline_exceeded", deadline_s=self._settings.auth_login_deadline_seconds
            )
            raise ServiceUnavailableError(
                "Login timed out waiting for the directory.",
                dependency="auth",
                reason="login_deadline",
            ) from exc

    async def _authenticate(self, username: str, password: str) -> Role | LoginResult | None:
        """
        Run the bind and the role checks, logging how the role was decided.

        Args:
            username (str): The `sAMAccountName` to bind and look up.
            password (str): The password to bind with.

        Returns:
            Role | LoginResult | None: As `authenticate`.
        """
        if not await ldap_validate(self._settings, username, password):
            return None

        started = time.monotonic()
        uname = username.strip().lower()
        admin_users = split_csv_lower(self._list("admin_users"))
        if uname in admin_users:
            self._log_role(Role.ADMIN, "admin_users", started)
            return Role.ADMIN
        if group := await self._first_matching_group(self._list("admin_groups"), uname):
            self._log_role(Role.ADMIN, f"group:{group}", started)
            return Role.ADMIN

        auditor_users = split_csv_lower(self._list("auditor_users"))
        if uname in auditor_users:
            self._log_role(Role.AUDITOR, "auditor_users", started)
            return Role.AUDITOR
        if group := await self._first_matching_group(self._list("auditor_groups"), uname):
            self._log_role(Role.AUDITOR, f"group:{group}", started)
            return Role.AUDITOR

        viewer_users = split_csv_lower(self._list("viewer_users"))
        if uname in viewer_users:
            self._log_role(Role.VIEWER, "viewer_users", started)
            return Role.VIEWER
        if group := await self._first_matching_group(self._list("view_groups"), uname):
            self._log_role(Role.VIEWER, f"group:{group}", started)
            return Role.VIEWER

        self._log_role(None, "no_match", started)
        return LoginResult.NO_PERMISSION

    @staticmethod
    def _log_role(role: Role | None, via: str, started: float) -> None:
        """Log the role decision and which list or group decided it."""
        logger.info(
            "auth.role_resolved",
            role=role.value if role else None,
            via=via,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )

    def _list(self, field: str) -> str:
        """
        Resolve one admin/viewer group-or-user CSV list, live file first.

        Args:
            field (str): `Settings` field name (`admin_groups`, `auditor_groups`,
                `view_groups`, `admin_users`, `auditor_users` or `viewer_users`).

        Returns:
            str: The mounted file's content if `<field>_file` is set and
                readable, else the static `Settings` value — so an operator
                can edit the ConfigMap and have the next login see it, with
                no API pod restart (deploy/README.md, "Configuration notes").
        """
        file_path = getattr(self._settings, f"{field}_file")
        if file_path:
            try:
                return Path(file_path).read_text()
            except OSError:
                pass
        return getattr(self._settings, field)

    async def _first_matching_group(self, groups_csv: str, uname: str) -> str | None:
        """Return the first configured group `uname` is a member of, or None."""
        for group in split_csv_lower(groups_csv):
            if uname in await self._ad_api.group_members(group):
                return group
        return None
