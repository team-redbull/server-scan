"""AD login orchestration: credentials, then admin-before-viewer group/user checks.

Rules (docs/adr/0034, from the operator's own AD-integration spec):

1. The LDAP bind happens first — a bad password never reaches the group checks.
2. Admin is checked before viewer, and a user's own allow-list is checked
   before its groups, so a listed user costs no AD API call. If a user is
   in both an admin and a viewer group, admin wins because admin is
   checked first and returns immediately.
3. Matching is case-insensitive throughout.
4. `ServiceUnavailableError` (LDAP or the AD API down) always propagates —
   it must never be reported as a wrong password.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.config.settings import Settings
from app.domain.models.audit_event import Role
from app.domain.services.authz import LoginResult, split_csv_lower
from app.infrastructure.ad.client import ldap_validate


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
            Role | LoginResult | None: `Role.ADMIN` or `Role.VIEWER` if
                authenticated and permitted, `LoginResult.NO_PERMISSION` if
                authenticated but listed nowhere, or None for a wrong
                username/password.

        Raises:
            ServiceUnavailableError: LDAP or the AD API is unreachable or
                misbehaved — never conflated with a wrong password.
        """
        if not await ldap_validate(self._settings, username, password):
            return None

        uname = username.strip().lower()
        admin_users = split_csv_lower(self._list("admin_users"))
        if uname in admin_users or await self._member_of_any(self._list("admin_groups"), uname):
            return Role.ADMIN

        viewer_users = split_csv_lower(self._list("viewer_users"))
        if uname in viewer_users or await self._member_of_any(self._list("view_groups"), uname):
            return Role.VIEWER

        return LoginResult.NO_PERMISSION

    def _list(self, field: str) -> str:
        """
        Resolve one admin/viewer group-or-user CSV list, live file first.

        Args:
            field (str): `Settings` field name (`admin_groups`,
                `view_groups`, `admin_users` or `viewer_users`).

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

    async def _member_of_any(self, groups_csv: str, uname: str) -> bool:
        """Check `uname` against each configured group, stopping at the first match."""
        for group in split_csv_lower(groups_csv):
            if uname in await self._ad_api.group_members(group):
                return True
        return False
