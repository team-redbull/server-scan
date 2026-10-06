"""LDAP credential check and AD API group-membership lookup (docs/adr/0034).

Split from `app.application.services.auth_service` because both calls are
infrastructure — one talks to a directory over LDAP, the other to a REST
service — and neither knows about roles; `AuthService` is what turns
"member of these groups" into a `Role`.
"""

from __future__ import annotations

import asyncio
import random
import time

import httpx
import structlog
from ldap3 import NONE, Connection, Server
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPException,
    LDAPResponseTimeoutError,
    LDAPSocketOpenError,
)

from app.config.settings import Settings
from app.errors import ServiceUnavailableError
from app.infrastructure.blocking import run_abandonable
from app.observability.metrics import dependency_call_duration_seconds

logger = structlog.get_logger(__name__)

_RETRYABLE_STATUS = frozenset({502, 503, 504})


def _ldap_failure_reason(exc: LDAPException) -> str:
    """
    Name why an LDAP call failed, as a short slug for logs and metrics.

    Args:
        exc (LDAPException): The ldap3 error.

    Returns:
        str: `connect_timeout`, `connect_failed`, `receive_timeout`, `receive_failed`,
            `bind_rejected` or `ldap_error`.
    """
    timed_out = isinstance(exc, TimeoutError | LDAPResponseTimeoutError)
    if isinstance(exc, LDAPSocketOpenError):
        return "connect_timeout" if timed_out else "connect_failed"
    if isinstance(exc, LDAPBindError):
        return "bind_rejected"
    if timed_out:
        return "receive_timeout"
    return "receive_failed" if "receiving" in str(exc) else "ldap_error"


def _ldap_bind_sync(settings: Settings, username: str, password: str) -> bool:
    server = Server(
        settings.ldap_server,
        port=settings.ldap_port,
        use_ssl=settings.ldap_use_ssl,
        get_info=NONE,
        connect_timeout=settings.auth_connect_timeout_seconds,
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
            receive_timeout=settings.ldap_receive_timeout_seconds,
        )
    except LDAPBindError as exc:
        if "invalidCredentials" in str(exc):
            return False
        raise ServiceUnavailableError(
            f"LDAP bind failed: {exc}", dependency="ldap", reason=_ldap_failure_reason(exc)
        ) from exc
    except LDAPException as exc:
        raise ServiceUnavailableError(
            f"LDAP connection failed: {exc}", dependency="ldap", reason=_ldap_failure_reason(exc)
        ) from exc
    else:
        connection.unbind()
        return True


async def ldap_validate(settings: Settings, username: str, password: str) -> bool:
    r"""
    Check `username`/`password` against LDAP by binding as `DOMAIN\username`.

    Args:
        settings (Settings): Supplies `ldap_server`/`ldap_port`/`ldap_domain`/`ldap_use_ssl`
            and the connect/receive timeouts.
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
    started = time.monotonic()
    outcome = "ok"
    try:
        # A blocking socket call off the loop, on a daemon thread (`app.infrastructure.blocking`);
        # the socket timeouts above are what end it, `asyncio.timeout` only abandons the await.
        valid = await run_abandonable(
            _ldap_bind_sync, settings, username, password, name="ldap-bind"
        )
    except ServiceUnavailableError as exc:
        outcome = exc.reason or "error"
        logger.warning(
            "ldap.bind_failed",
            reason=outcome,
            detail=str(exc)[:300],
            server=settings.ldap_server,
            duration_ms=_elapsed_ms(started),
        )
        raise
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    else:
        outcome = "ok" if valid else "invalid_credentials"
        logger.info("ldap.bind", outcome=outcome, duration_ms=_elapsed_ms(started))
        return valid
    finally:
        dependency_call_duration_seconds.labels(dependency="ldap", outcome=outcome).observe(
            time.monotonic() - started
        )


def _elapsed_ms(started: float) -> float:
    """
    Milliseconds since `started`.

    Args:
        started (float): A `time.monotonic()` reading.

    Returns:
        float: Elapsed milliseconds, rounded to 0.1.
    """
    return round((time.monotonic() - started) * 1000, 1)


def _ad_api_failure(exc: Exception) -> tuple[str, bool]:
    """
    Classify an AD API failure.

    Args:
        exc (Exception): What the request raised.

    Returns:
        tuple[str, bool]: A short reason slug (`read_timeout`, `connect_timeout`,
            `connect_error`, `http_503`, `invalid_json`, ...) and whether
            retrying the same GET could help.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return f"http_{status}", status in _RETRYABLE_STATUS
    if isinstance(exc, httpx.TimeoutException):
        kind = type(exc).__name__.removesuffix("Timeout").lower()
        return f"{kind}_timeout", True
    if isinstance(exc, httpx.TransportError):
        return "connect_error" if isinstance(exc, httpx.ConnectError) else "transport_error", True
    return "http_error", False


class AdApiClient:
    """Recursive AD group membership, via the operator's own AD API.

    Takes a pre-built `httpx.AsyncClient`, testable with `httpx.MockTransport`
    like `app.infrastructure.openshift.client.InClusterClient`.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        client_id: str,
        retries: int = 0,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        """
        Build the client.

        Args:
            http (httpx.AsyncClient): Pre-configured with `base_url`,
                timeout and TLS verification.
            client_id (str): The `ClientId` header value.
            retries (int): Extra attempts after a timeout, a connection
                error or a 502/503/504 — never after a 4xx or a bad body.
            retry_delay_seconds (float): Upper bound of the jittered pause before a retry.
        """
        self._http = http
        self._client_id = client_id
        self._retries = retries
        self._retry_delay = retry_delay_seconds

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
        attempts = self._retries + 1
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                members = await self._fetch(group_sam)
            except _AdApiUnavailable as exc:
                reason = exc.reason or "error"
                will_retry = attempt < attempts and exc.retryable
                dependency_call_duration_seconds.labels(
                    dependency="ad_api", outcome=reason
                ).observe(time.monotonic() - started)
                logger.warning(
                    "ad_api.attempt_failed",
                    group=group_sam,
                    attempt=attempt,
                    of=attempts,
                    reason=reason,
                    detail=str(exc)[:300],
                    duration_ms=_elapsed_ms(started),
                    will_retry=will_retry,
                )
                if not will_retry:
                    raise
                await asyncio.sleep(random.uniform(self._retry_delay / 2, self._retry_delay))  # noqa: S311
            else:
                dependency_call_duration_seconds.labels(dependency="ad_api", outcome="ok").observe(
                    time.monotonic() - started
                )
                logger.info(
                    "ad_api.group_members",
                    group=group_sam,
                    members=len(members),
                    attempt=attempt,
                    duration_ms=_elapsed_ms(started),
                )
                return members
        raise AssertionError("unreachable: the last failed attempt re-raises")  # pragma: no cover

    async def _fetch(self, group_sam: str) -> set[str]:
        """
        Make one attempt at `group_members`.

        Args:
            group_sam (str): The group's `sAMAccountName`.

        Returns:
            set[str]: Lower-cased member usernames.

        Raises:
            _AdApiUnavailable: The attempt failed; `.retryable` says whether another could help.
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
            reason, retryable = _ad_api_failure(exc)
            cause = f"{type(exc).__name__} {exc}".strip()
            raise _AdApiUnavailable(
                f"AD API request failed for group '{group_sam}': {cause}",
                reason,
                retryable=retryable,
            ) from exc
        except ValueError as exc:
            raise _AdApiUnavailable(
                f"AD API returned invalid JSON for group '{group_sam}': {exc}", "invalid_json"
            ) from exc

        if not isinstance(members, list):
            raise _AdApiUnavailable(
                f"AD API returned a non-array body for group '{group_sam}'.", "invalid_body"
            )
        return {
            str(member["sAMAccountName"]).strip().lower()
            for member in members
            if isinstance(member, dict) and member.get("sAMAccountName")
        }


class _AdApiUnavailable(ServiceUnavailableError):
    """An AD API failure that knows whether the same GET could succeed on a retry."""

    def __init__(self, detail: str, reason: str, *, retryable: bool = False) -> None:
        """
        Build the error.

        Args:
            detail (str): Human-readable explanation.
            reason (str): Short slug for the log and metric.
            retryable (bool): Whether the same request could succeed on a retry.
        """
        super().__init__(detail, dependency="ad_api", reason=reason)
        self.retryable = retryable


def build_ad_api_http_client(settings: Settings) -> httpx.AsyncClient:
    """
    Build the `httpx.AsyncClient` `AdApiClient` talks through.

    Args:
        settings (Settings): Supplies `ad_api_url`/`ad_api_ca_bundle`/
            `ad_api_verify_tls` and the connect/request timeouts.

    Returns:
        httpx.AsyncClient: Not yet opened — use as an `async with` context
            (`app.api.v1.auth._auth_service` does, once per login request).
    """
    verify: bool | str = (
        False if not settings.ad_api_verify_tls else settings.ad_api_ca_bundle or True
    )
    return httpx.AsyncClient(
        base_url=settings.ad_api_url.rstrip("/"),
        timeout=httpx.Timeout(
            connect=settings.auth_connect_timeout_seconds,
            read=settings.auth_request_timeout_seconds,
            write=settings.auth_request_timeout_seconds,
            pool=settings.auth_connect_timeout_seconds,
        ),
        verify=verify,
    )
