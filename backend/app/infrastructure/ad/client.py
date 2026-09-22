"""LDAP credential check and AD API group-membership lookup (docs/adr/0034).

Split from `app.application.services.auth_service` because both calls are
infrastructure — one talks to a directory over LDAP, the other to a REST
service — and neither knows about roles; `AuthService` is what turns
"member of these groups" into a `Role`.
"""

from __future__ import annotations

import asyncio

import httpx
from ldap3 import ALL, Connection, Server
from ldap3.core.exceptions import LDAPBindError, LDAPException

from app.config.settings import Settings
from app.errors import ServiceUnavailableError


def _ldap_bind_sync(settings: Settings, username: str, password: str) -> bool:
    server = Server(
        settings.ldap_server, port=settings.ldap_port, use_ssl=settings.ldap_use_ssl, get_info=ALL
    )
    try:
        # A successful bind with the user's own account + password IS the
        # credential check — no search, no service account.
        connection = Connection(
            server,
            user=f"{settings.ldap_domain}\\{username}",
            password=password,
            auto_bind=True,
            read_only=True,
        )
    except LDAPBindError as exc:
        if "invalidCredentials" in str(exc):
            return False
        raise ServiceUnavailableError(f"LDAP bind failed: {exc}") from exc
    except LDAPException as exc:
        raise ServiceUnavailableError(f"LDAP connection failed: {exc}") from exc
    else:
        connection.unbind()
        return True


async def ldap_validate(settings: Settings, username: str, password: str) -> bool:
    r"""
    Check `username`/`password` against LDAP by binding as `DOMAIN\username`.

    Args:
        settings (Settings): Supplies `ldap_server`/`ldap_port`/`ldap_domain`/`ldap_use_ssl`.
        username (str): The `sAMAccountName` to bind as.
        password (str): The password to bind with.

    Returns:
        bool: True if the bind succeeded, False on a wrong username/password
            or a disabled account.

    Raises:
        ServiceUnavailableError: The LDAP server is unreachable or misbehaved
            — never returned as False, so a caller can't mistake it for a
            wrong password.
    """
    if not username or not password:
        return False
    # ldap3's bind is a blocking socket call; this coroutine must not block
    # the event loop for every other in-flight request while it runs.
    return await asyncio.to_thread(_ldap_bind_sync, settings, username, password)


class AdApiClient:
    """Recursive AD group membership, via the operator's own AD API.

    Takes a pre-built `httpx.AsyncClient`, testable with `httpx.MockTransport`
    like `app.infrastructure.openshift.client.InClusterClient`.
    """

    def __init__(self, http: httpx.AsyncClient, *, client_id: str) -> None:
        """
        Build the client.

        Args:
            http (httpx.AsyncClient): Pre-configured with `base_url`,
                timeout and TLS verification.
            client_id (str): The `ClientId` header value.
        """
        self._http = http
        self._client_id = client_id

    async def group_members(self, group_sam: str) -> set[str]:
        """
        Return the lower-cased `sAMAccountName`s of every recursive member of `group_sam`.

        Args:
            group_sam (str): The group's `sAMAccountName`.

        Returns:
            set[str]: Lower-cased member usernames.

        Raises:
            ServiceUnavailableError: The AD API is unreachable, returned a
                non-2xx response, or returned a body that isn't the
                documented JSON array — never returned as an empty set, so a
                caller can't mistake "directory is down" for "not a member".
        """
        try:
            response = await self._http.get(
                "/groups/members/user",
                params={"samAccountName": group_sam, "isRecursive": "true"},
                headers={"accept": "application/json", "ClientId": self._client_id},
            )
            response.raise_for_status()
            members = response.json()
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                f"AD API request failed for group '{group_sam}': {exc}"
            ) from exc
        except ValueError as exc:
            raise ServiceUnavailableError(
                f"AD API returned invalid JSON for group '{group_sam}': {exc}"
            ) from exc

        if not isinstance(members, list):
            raise ServiceUnavailableError(
                f"AD API returned a non-array body for group '{group_sam}'."
            )
        return {
            str(member["sAMAccountName"]).strip().lower()
            for member in members
            if isinstance(member, dict) and member.get("sAMAccountName")
        }


def build_ad_api_http_client(settings: Settings) -> httpx.AsyncClient:
    """
    Build the `httpx.AsyncClient` `AdApiClient` talks through.

    Args:
        settings (Settings): Supplies `ad_api_url`/`ad_api_ca_bundle`/
            `auth_request_timeout_seconds`.

    Returns:
        httpx.AsyncClient: Not yet opened — use as an `async with` context
            (`app.api.v1.auth._auth_service` does, once per login request).
    """
    return httpx.AsyncClient(
        base_url=settings.ad_api_url.rstrip("/"),
        timeout=settings.auth_request_timeout_seconds,
        verify=settings.ad_api_ca_bundle or True,
    )
