"""Async Redfish client for one BMC, built on the `httpx` this project already pins.

Written rather than taken from a library — see docs/adr/0016 — because the
DMTF library hardcodes `verify = False` and logs the session token
unredacted, and `sushy` is sync-only and untyped; what carries over from
`sushy` is its field knowledge (`Connection: close`, split connect/read
timeouts, never retrying an `SSLError`), not its code. The exception
hierarchy is wider than the Cisco clients' single type on purpose: a
rejected login and an unreachable BMC must drive different retry
behaviour, since retrying a rejected login across an estate locks accounts.
"""

from __future__ import annotations

import asyncio
import random
import ssl
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from app.infrastructure.providers.redfish.targets import RedfishTarget

logger = structlog.get_logger(__name__)

_ODATA_ROOT = "/redfish/v1"
_SERVICE_ROOT = "/redfish/v1/"
_MAX_ATTEMPTS = 3
_RETRY_STATUSES = frozenset({429, 503})
# See ADR-0016's 2026-09-13 update for the cap's reasoning.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

_TLS_VERSIONS = {
    "TLSv1": ssl.TLSVersion.TLSv1,
    "TLSv1_1": ssl.TLSVersion.TLSv1_1,
    "TLSv1_2": ssl.TLSVersion.TLSv1_2,
    "TLSv1_3": ssl.TLSVersion.TLSv1_3,
}


class RedfishError(Exception):
    """Any failure talking to one BMC."""


class RedfishUnreachableError(RedfishError):
    """DNS, refused, timed out, or a transport-level protocol failure. Retryable."""


class RedfishTlsError(RedfishError):
    """Certificate verification or TLS handshake failure.

    Never retried: sushy's rule, and its reasoning is right — this is a
    configuration problem, not a transient one.
    """


class RedfishAuthError(RedfishError):
    """The BMC rejected the credential (401, or 403 on session creation).

    Never retried; counted in the run summary's `auth_failures`.
    """


class RedfishProtocolError(RedfishError):
    """Reachable and authenticated, but the response is unusable.

    Also raised for a service that is not conformant Redfish at all.
    """


class RedfishForbiddenError(RedfishError):
    """A resource returned 403 after a successful login.

    Not a `RedfishAuthError`: a ReadOnly account legitimately gets 403 on
    some vendors' resources (ADR-0016, 2026-09-13 update).
    """


def build_ssl_context(target: RedfishTarget, *, min_version: str) -> ssl.SSLContext | bool:
    """
    Build the TLS configuration for one BMC.

    Args:
        target (RedfishTarget): The host, carrying its own verification
            settings.
        min_version (str): Minimum TLS version name, e.g. `"TLSv1_2"`.

    Returns:
        ssl.SSLContext | bool: A configured context, or `False` for a host
            whose operator explicitly opted out of verification.
    """
    if not target.verify_tls:
        # The one TLS opt-out; ruff S501 cannot see it — ADR-0016, 2026-09-13 update.
        return False
    context = ssl.create_default_context(cafile=target.ca_bundle or None)
    context.minimum_version = _TLS_VERSIONS.get(min_version, ssl.TLSVersion.TLSv1_2)
    return context


def validate_odata_id(odata_id: object) -> str:
    """
    Check that a link the BMC handed us points back into its own tree.

    An absolute or traversing `@odata.id` would retarget the next request,
    session token included (ADR-0016).

    Args:
        odata_id (object): The `@odata.id` value as received.

    Returns:
        str: The validated path.

    Raises:
        RedfishProtocolError: If it is missing, not a string, not rooted
            at `/redfish/v1`, or contains a traversal segment.
    """
    if not isinstance(odata_id, str) or not odata_id:
        raise RedfishProtocolError(f"@odata.id is missing or not a string: {odata_id!r}")
    if not odata_id.startswith(_ODATA_ROOT):
        raise RedfishProtocolError(
            f"@odata.id {odata_id!r} is not a relative path under {_ODATA_ROOT} — refusing to "
            "follow it, since the session token travels with every request."
        )
    if ".." in odata_id.split("/"):
        raise RedfishProtocolError(f"@odata.id {odata_id!r} contains a traversal segment.")
    return odata_id


class RedfishClient:
    """
    One authenticated Redfish session against one BMC.

    An async context manager: `__aenter__` probes the service root and logs
    in, `__aexit__` logs out. See docs/adr/0016-redfish-standalone-collector.md.
    """

    def __init__(
        self,
        *,
        target: RedfishTarget,
        connect_timeout: float,
        read_timeout: float,
        tls_min_version: str = "TLSv1_2",
        debug_http: bool = False,
    ) -> None:
        """
        Build a client for one BMC. No I/O happens here.

        Args:
            target (RedfishTarget): Host, credential and TLS settings.
            connect_timeout (float): Seconds to establish a connection.
            read_timeout (float): Seconds to wait for a response body.
            tls_min_version (str): Minimum TLS version name.
            debug_http (bool): Emit one redacted line per request.
        """
        self._target = target
        self._debug_http = debug_http
        self._token: str | None = None
        self._session_uri: str | None = None
        self.service_root: dict[str, Any] = {}
        self._client = httpx.AsyncClient(
            base_url=target.base_url,
            verify=build_ssl_context(target, min_version=tls_min_version),
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
            ),
            # The zero-keepalive pool is what makes `Connection: close` real.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            headers={
                "OData-Version": "4.0",
                "Accept": "application/json",
                "Connection": "close",
            },
            # A 3xx would retarget the next request, token included — ADR-0016.
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        """
        Probe the service root, confirm it is conformant Redfish, and open a session.

        Returns:
            Self: The authenticated client.

        Raises:
            RedfishUnreachableError: If the BMC cannot be reached.
            RedfishTlsError: If its certificate cannot be verified.
            RedfishProtocolError: If it is not conformant Redfish.
            RedfishAuthError: If the credential is rejected.
        """
        try:
            self.service_root = await self._request("GET", _SERVICE_ROOT, authenticated=False)
            self._assert_conformant(self.service_root)
            await self._login()
        except BaseException:
            await self._client.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """
        Delete the session, shielded from cancellation, and close the transport.

        See ADR-0016's 2026-09-13 update for why.

        Raises:
            asyncio.CancelledError: Re-raised after logging and closing the
                transport, never swallowed — `provider.py` cancels and
                drains this task on an early stop, and swallowing it here
                would make the task appear to finish normally.
        """
        try:
            if self._session_uri is not None:
                await asyncio.shield(asyncio.wait_for(self._logout(), timeout=10.0))
        except Exception as exc_info:
            logger.warning("redfish.logout_failed", host=self._target.host, error=str(exc_info))
        except asyncio.CancelledError:
            logger.warning("redfish.logout_cancelled", host=self._target.host)
            raise
        finally:
            await self._client.aclose()

    def _assert_conformant(self, root: dict[str, Any]) -> None:
        """
        Reject a pre-Redfish service by shape, before any credential is sent.

        HPE iLO 4 and any equally divergent BMC (ADR-0016, "What is still unproven").

        Args:
            root (dict[str, Any]): The service root payload.

        Raises:
            RedfishProtocolError: If the payload is not conformant.
        """
        odata_type = str(root.get("@odata.type", ""))
        if "RedfishVersion" not in root or ".v1_" not in odata_type:
            raise RedfishProtocolError(
                f"{self._target.host} does not answer /redfish/v1 with a conformant Redfish "
                f"service root (@odata.type={odata_type or 'missing'!r}, "
                f"RedfishVersion={root.get('RedfishVersion', 'missing')!r}). Pre-Redfish "
                "services such as HPE iLO 4 are out of scope."
            )

    def _sessions_uri(self) -> str:
        """
        Where to POST to create a session, per DSP0266 §13.3.4.1 — never hardcoded.

        Returns:
            str: The sessions collection path.

        Raises:
            RedfishProtocolError: If the service root advertises neither.
        """
        links = self.service_root.get("Links", {})
        for candidate in (
            links.get("Sessions", {}).get("@odata.id") if isinstance(links, dict) else None,
            self.service_root.get("SessionService", {}).get("@odata.id"),
        ):
            if candidate:
                return validate_odata_id(candidate)
        raise RedfishProtocolError(
            f"{self._target.host} advertises no Sessions collection in its service root."
        )

    async def _login(self) -> None:
        """
        Create a session and capture its token and location.

        Raises:
            RedfishAuthError: If the credential is rejected.
            RedfishProtocolError: If the response carries no token.
        """
        # No trailing slash appended — ADR-0016's first 2026-08-23 update.
        response = await self._send(
            "POST",
            self._sessions_uri(),
            authenticated=False,
            json={
                "UserName": self._target.credential.username,
                "Password": self._target.credential.password,
            },
        )
        if response.status_code in (401, 403):
            raise RedfishAuthError(
                f"{self._target.host} rejected credential "
                f"{self._target.credential.name!r} ({response.status_code})"
            )
        if response.status_code >= 400:
            raise RedfishProtocolError(
                f"{self._target.host} refused session creation with HTTP {response.status_code}"
            )
        token = response.headers.get("X-Auth-Token")
        if not token:
            raise RedfishProtocolError(
                f"{self._target.host} created a session but returned no X-Auth-Token."
            )
        self._token = token
        self._session_uri = response.headers.get("Location") or None

    async def _logout(self) -> None:
        """Delete this client's session. Best effort."""
        if self._session_uri is None:
            return
        path = self._session_uri
        if path.startswith("http"):
            path = httpx.URL(path).path
        await self._send("DELETE", path, authenticated=True)

    async def get(self, path: str) -> dict[str, Any]:
        """
        Fetch one resource.

        Args:
            path (str): A validated `@odata.id`.

        Returns:
            dict[str, Any]: The parsed payload.

        Raises:
            RedfishError: Per the module's exception hierarchy.
        """
        return await self._request("GET", path, authenticated=True)

    async def get_collection(self, path: str) -> list[dict[str, Any]]:
        """
        Fetch every member of a collection by following `Members@odata.nextLink`.

        `Members@odata.count` is ignored (ADR-0016, 2026-09-13 update).

        Args:
            path (str): The collection's `@odata.id`.

        Returns:
            list[dict[str, Any]]: Every member's payload.

        Raises:
            RedfishError: Per the module's exception hierarchy.
        """
        members: list[dict[str, Any]] = []
        next_path: str | None = validate_odata_id(path)
        while next_path:
            page = await self.get(next_path)
            for member in page.get("Members", []) or []:
                if not isinstance(member, dict):
                    continue
                if "@odata.type" in member:
                    members.append(member)
                else:
                    members.append(await self.get(validate_odata_id(member.get("@odata.id"))))
            raw_next = page.get("Members@odata.nextLink")
            next_path = validate_odata_id(raw_next) if raw_next else None
        return members

    async def _request(self, method: str, path: str, *, authenticated: bool) -> dict[str, Any]:
        """
        Issue a request and parse its JSON body.

        Args:
            method (str): HTTP method.
            path (str): Request path.
            authenticated (bool): Whether to send the session token.

        Returns:
            dict[str, Any]: The parsed payload.

        Raises:
            RedfishAuthError: On a 401.
            RedfishForbiddenError: On a 403 for a resource.
            RedfishProtocolError: On any other error status, a redirect,
                an oversized body, or unparseable JSON.
        """
        response = await self._send(method, path, authenticated=authenticated)
        if response.status_code == 401:
            raise RedfishAuthError(f"{self._target.host} returned 401 for {path}")
        if response.status_code == 403:
            raise RedfishForbiddenError(f"{self._target.host} returned 403 for {path}")
        if 300 <= response.status_code < 400:
            raise RedfishProtocolError(
                f"{self._target.host} redirected {path} (HTTP {response.status_code}); "
                "redirects are never followed, since the session token travels with the request."
            )
        if response.status_code >= 400:
            raise RedfishProtocolError(
                f"{self._target.host} returned HTTP {response.status_code} for {path}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RedfishProtocolError(f"{self._target.host} returned non-JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise RedfishProtocolError(
                f"{self._target.host} returned a non-object payload for {path}"
            )
        return payload

    async def _send(
        self, method: str, path: str, *, authenticated: bool, json: dict[str, str] | None = None
    ) -> httpx.Response:
        """
        Send one request, retrying only transport-level failures.

        Never a 4xx, never an `SSLError` (ADR-0016).

        Args:
            method (str): HTTP method.
            path (str): Request path.
            authenticated (bool): Whether to send the session token.
            json (dict[str, str] | None): Request body, if any.

        Returns:
            httpx.Response: The response, whatever its status.

        Raises:
            RedfishTlsError: On a certificate or handshake failure.
            RedfishUnreachableError: If the host stays unreachable.
        """
        headers = {"X-Auth-Token": self._token} if authenticated and self._token else None
        last: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.request(method, path, headers=headers, json=json)
            except httpx.ConnectError as exc:
                if isinstance(exc.__cause__, ssl.SSLError):
                    raise RedfishTlsError(
                        f"TLS verification failed for {self._target.host}: {exc}. Point "
                        "INVENTORY_REDFISH_CA_BUNDLE at the issuing CA, or set "
                        "`verify_tls = false` with a reason for this host."
                    ) from exc
                last = exc
            except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
                last = exc
            else:
                self._trace(method, path, response.status_code)
                if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                self._guard_size(response, path)
                return response
            if attempt < _MAX_ATTEMPTS:
                await self._backoff(attempt, None)
        raise RedfishUnreachableError(f"Could not reach {self._target.host}: {last}")

    def _guard_size(self, response: httpx.Response, path: str) -> None:
        """
        Refuse a response too large to hold in memory.

        Checked after httpx has decompressed it (ADR-0016, 2026-09-13 update).

        Args:
            response (httpx.Response): The response to check.
            path (str): Request path, for the message.

        Raises:
            RedfishProtocolError: If the body exceeds the cap.
        """
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise RedfishProtocolError(
                f"{self._target.host} returned {len(response.content)} bytes for {path}, "
                f"over the {_MAX_RESPONSE_BYTES} byte cap."
            )

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """
        Wait before retrying a transport failure.

        Args:
            attempt (int): 1-based attempt just completed.
            retry_after (str | None): The response's `Retry-After`, if any.
        """
        if retry_after and retry_after.isdigit():
            await asyncio.sleep(min(float(retry_after), 30.0))
            return
        await asyncio.sleep(min(2.0**attempt, 8.0) + random.uniform(0, 0.5))  # noqa: S311

    def _trace(self, method: str, path: str, status: int) -> None:
        """
        Emit one debug line per request.

        Method, path and status only, and never for the session exchange
        (ADR-0016, 2026-09-13 update).

        Args:
            method (str): HTTP method.
            path (str): Request path.
            status (int): Response status code.
        """
        if not self._debug_http or "SessionService" in path or "Sessions" in path:
            return
        logger.debug(
            "redfish.http", host=self._target.host, method=method, path=path, status=status
        )
