"""Async HTTP Basic client for one kubevirt-redfish endpoint.

Not `RedfishClient`: the facade has no `RedfishVersion` and no Sessions, and
answers every request with HTTP Basic (docs/adr/0058). The exception
hierarchy is `RedfishClient`'s, so callers classify failures the same way.
A 401 is never retried: the facade blocks an IP after 10 failed logins in 5 minutes.
"""

from __future__ import annotations

import asyncio
import random
import ssl
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx
import structlog

from app.infrastructure.providers.redfish.client import (
    RedfishAuthError,
    RedfishForbiddenError,
    RedfishProtocolError,
    RedfishTlsError,
    RedfishUnreachableError,
    validate_odata_id,
)

logger = structlog.get_logger(__name__)

SERVICE_ROOT = "/redfish/v1/"
SYSTEMS = "/redfish/v1/Systems"
_MAX_ATTEMPTS = 3
_RETRY_STATUSES = frozenset({429, 503})
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_RETRY_AFTER_SECONDS = 30.0


class NotFoundError(RedfishProtocolError):
    """The facade answered 404: the VM is gone, or not this user's to see."""


def origin_of(url: str) -> tuple[str, str]:
    """
    Split a configured facade URL into its origin and its `host[:port]`.

    Args:
        url (str): `INVENTORY_KUBEVIRT_URL`, e.g. `https://host` or `https://host/redfish/v1`.

    Returns:
        tuple[str, str]: `("https://host:8443", "host:8443")`; any path is dropped.

    Raises:
        ValueError: If the scheme is not http(s) or there is no host.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"INVENTORY_KUBEVIRT_URL must be an http(s) URL with a host, got {url!r}.")
    return f"{parts.scheme}://{parts.netloc}", parts.netloc


def build_verify(*, verify_tls: bool, ca_bundle: str) -> ssl.SSLContext | bool:
    """
    Build the TLS configuration for the facade.

    Args:
        verify_tls (bool): False is the documented opt-out; the provider logs it.
        ca_bundle (str): A PEM file to trust instead of the system store, or "".

    Returns:
        ssl.SSLContext | bool: A verifying context, or False.
    """
    if not verify_tls:
        return False
    context = ssl.create_default_context(cafile=ca_bundle or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class KubeVirtClient:
    """One kubevirt-redfish endpoint, every request carrying HTTP Basic credentials."""

    def __init__(
        self,
        *,
        origin: str,
        username: str,
        password: str,
        verify: ssl.SSLContext | bool,
        connect_timeout: float,
        read_timeout: float,
        max_connections: int = 4,
        debug_http: bool = False,
    ) -> None:
        """
        Build a client. No I/O happens here.

        Args:
            origin (str): `scheme://host[:port]`, from `origin_of`.
            username (str): The facade user.
            password (str): Its password.
            verify (ssl.SSLContext | bool): From `build_verify`.
            connect_timeout (float): Seconds to establish a connection.
            read_timeout (float): Seconds to wait for a response.
            max_connections (int): Pool size; the provider's concurrency.
            debug_http (bool): Emit one line per request (method, path, status).
        """
        self._origin = origin
        self._debug_http = debug_http
        self._client = httpx.AsyncClient(
            base_url=origin,
            auth=httpx.BasicAuth(username, password),
            verify=verify,
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=read_timeout, pool=None
            ),
            limits=httpx.Limits(max_connections=max(1, max_connections)),
            headers={"OData-Version": "4.0", "Accept": "application/json"},
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        """
        Enter the client.

        Returns:
            Self: This client.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """
        Close the transport.

        Args:
            exc_type (type[BaseException] | None): Unused.
            exc (BaseException | None): Unused.
            tb (TracebackType | None): Unused.
        """
        await self._client.aclose()

    async def get(self, path: str, *, authenticated: bool = True) -> dict[str, Any]:
        """
        Fetch one resource.

        Args:
            path (str): An `@odata.id` under `/redfish/v1`.
            authenticated (bool): False sends no credentials (the open service root).

        Returns:
            dict[str, Any]: The parsed JSON object.

        Raises:
            RedfishAuthError: On 401, after exactly one request.
            RedfishForbiddenError: On 403.
            NotFoundError: On 404.
            RedfishProtocolError: On any other error status, a redirect, an
                oversized or non-object body.
            RedfishTlsError: On a certificate failure.
            RedfishUnreachableError: When the facade cannot be reached.
        """
        path = validate_odata_id(path)
        response = await self._send(path, authenticated=authenticated)
        status = response.status_code
        if status == 401:
            raise RedfishAuthError(
                f"{self._origin} rejected the KubeVirt credential (401) for {path}"
            )
        if status == 403:
            raise RedfishForbiddenError(f"{self._origin} returned 403 for {path}")
        if status == 404:
            raise NotFoundError(f"{self._origin} returned 404 for {path}", status_code=404)
        if 300 <= status < 400:
            raise RedfishProtocolError(
                f"{self._origin} redirected {path} (HTTP {status}); redirects are never followed, "
                "since the credentials travel with every request.",
                status_code=status,
            )
        if status >= 400:
            raise RedfishProtocolError(
                f"{self._origin} returned HTTP {status} for {path}", status_code=status
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise RedfishProtocolError(
                f"{self._origin} returned {len(response.content)} bytes for {path}, over the "
                f"{_MAX_RESPONSE_BYTES} byte cap."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RedfishProtocolError(f"{self._origin} returned non-JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise RedfishProtocolError(f"{self._origin} returned a non-object payload for {path}")
        return payload

    async def get_collection(self, path: str) -> list[dict[str, Any]]:
        """
        Fetch a collection and every member it links to.

        Args:
            path (str): The collection's `@odata.id`.

        Returns:
            list[dict[str, Any]]: Each member's payload, in collection order.

        Raises:
            RedfishError: As `get`, for the collection or any member.
        """
        page = await self.get(path)
        return [await self.get(link) for link in member_links(page)]

    async def _send(self, path: str, *, authenticated: bool) -> httpx.Response:
        """
        Send one GET, retrying a connection failure, a 429 and a 503 only.

        A read timeout is not retried: the facade received the request, and the
        Route answers a stall with its own 504 at 30 s.

        Args:
            path (str): Request path.
            authenticated (bool): Whether to send the Basic credentials.

        Returns:
            httpx.Response: The response, whatever its status.

        Raises:
            RedfishTlsError: On a certificate or handshake failure.
            RedfishUnreachableError: If the facade stays unreachable.
        """
        last: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(
                    path, auth=None if not authenticated else httpx.USE_CLIENT_DEFAULT
                )
            except httpx.ConnectError as exc:
                if isinstance(exc.__cause__, ssl.SSLError) or "CERTIFICATE" in str(exc).upper():
                    raise RedfishTlsError(
                        f"TLS verification failed for {self._origin}: {exc}. Point "
                        "INVENTORY_KUBEVIRT_CA_BUNDLE at the ingress CA."
                    ) from exc
                last = exc
            except (httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                last = exc
            except httpx.TimeoutException as exc:
                raise RedfishUnreachableError(f"{self._origin} timed out on {path}: {exc}") from exc
            except httpx.TransportError as exc:
                last = exc
            else:
                self._trace(path, response.status_code)
                if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                return response
            if attempt < _MAX_ATTEMPTS:
                await self._backoff(attempt, None)
        raise RedfishUnreachableError(f"Could not reach {self._origin}: {last}")

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """
        Wait before a retry.

        Args:
            attempt (int): 1-based attempt just completed.
            retry_after (str | None): The response's `Retry-After` seconds, if any.
        """
        if retry_after and retry_after.isdigit():
            await asyncio.sleep(min(float(retry_after), _MAX_RETRY_AFTER_SECONDS))
            return
        await asyncio.sleep(min(2.0**attempt, 8.0) + random.uniform(0, 0.5))  # noqa: S311

    def _trace(self, path: str, status: int) -> None:
        """
        Emit one debug line per request when `--debug-http` is on.

        Args:
            path (str): Request path.
            status (int): Response status.
        """
        if self._debug_http:
            logger.debug("kubevirt.http", origin=self._origin, path=path, status=status)


def member_links(collection: dict[str, Any]) -> list[str]:
    """
    The validated `Members[].@odata.id` of a collection payload.

    Args:
        collection (dict[str, Any]): A Redfish collection.

    Returns:
        list[str]: Each member's path; a malformed entry is skipped.

    Raises:
        RedfishProtocolError: If a member link points outside `/redfish/v1`.
    """
    members = collection.get("Members")
    return [
        validate_odata_id(member["@odata.id"])
        for member in (members if isinstance(members, list) else [])
        if isinstance(member, dict) and member.get("@odata.id")
    ]
