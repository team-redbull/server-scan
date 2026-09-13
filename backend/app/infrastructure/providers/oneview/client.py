"""Async HTTP client for one HPE OneView appliance.

Hand-rolled on `httpx` rather than the `hpeOneView` SDK: the protocol is
one POST for a token, one header, and paginated GETs, while the SDK is
synchronous, depends on `future` and pins `docutils<0.18` across the
whole environment. See docs/adr/0022-oneview-only-hpe-collector.md,
"Decision 2".

Three behaviours here are not obvious and are the ones that break a
collector silently rather than loudly:

* `X-Api-Version` is required on every call, discovered from the
  unauthenticated `GET /rest/version`, and **clamped** to the newest
  version this code was written against.
* `count=-1` means *64* on `/rest/server-profiles`, not "all", so an
  explicit page size is always sent.
* A page loop that trusts `nextPageUri` alone cannot tell truncation
  from completion, so `get_all` compares what it fetched against the
  collection's own `total` and logs loudly when they disagree.

All three are documented in docs/hpe-collectors.md with their sources.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

logger = structlog.get_logger(__name__)

# OneView 10.20; newer appliances are clamped to it — docs/hpe-collectors.md, "API version".
MAX_TESTED_API_VERSION = 8000

# Never `-1` (that means 64) — docs/hpe-collectors.md, "Pagination, and the 256 ceiling".
DEFAULT_PAGE_SIZE = 256

# Small on purpose, and not a setting — docs/hpe-collectors.md, "Subresources".
EXPANDED_PAGE_SIZE = 25


class OneViewConnectionError(Exception):
    """
    Any failure talking to a OneView appliance.

    Rejected credentials, a non-2xx response, or an unreachable appliance
    — one exception type per vendor.
    """


class OneViewClient:
    """
    One authenticated OneView session, held for a single appliance's run.

    Use as an async context manager so the session is deleted on the
    appliance when the run ends (docs/hpe-collectors.md, "Sessions").
    """

    def __init__(
        self,
        *,
        endpoint: str,
        username: str,
        password: str,
        timeout_seconds: float,
        api_version: int = 0,
        verify_tls: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Build a client for one OneView appliance.

        Args:
            endpoint (str): Bare hostname or IP of the appliance.
            username (str): OneView account name.
            password (str): That account's password.
            timeout_seconds (float): Per-request timeout.
            api_version (int): `X-Api-Version` to pin. `0` discovers it
                from the appliance and clamps it to
                `MAX_TESTED_API_VERSION`; a non-zero value is used as
                given, after being checked against the appliance's own
                supported range.
            verify_tls (bool): Whether to verify the appliance's TLS
                certificate. Defaults to `False`: an appliance in an
                air-gapped estate ships a self-signed certificate with no
                private CA to trust it against.
            transport (httpx.AsyncBaseTransport | None): Injected
                transport, for tests. `None` uses the real network.

        Raises:
            ValueError: If `endpoint` is empty.
        """
        host = endpoint.strip()
        if not host:
            raise ValueError("OneView endpoint is empty.")
        self._endpoint = host
        self._username = username
        self._password = password
        self._configured_api_version = api_version
        self._api_version: int | None = None
        self._session_id: str | None = None
        # One message per short `get_all` — docs/hpe-collectors.md, "Pagination".
        self.truncations: list[str] = []
        self._http = httpx.AsyncClient(
            base_url=f"https://{host}",
            timeout=timeout_seconds,
            verify=verify_tls,
            transport=transport,
        )

    @property
    def endpoint(self) -> str:
        """The appliance this client talks to."""
        return self._endpoint

    @property
    def api_version(self) -> int:
        """
        The `X-Api-Version` this session negotiated.

        Returns:
            int: The pinned version, or `0` before `login()` has run.
        """
        return self._api_version or 0

    async def __aenter__(self) -> OneViewClient:
        """
        Open the OneView session.

        Returns:
            OneViewClient: This client, logged in and ready to query.

        Raises:
            OneViewConnectionError: If version discovery, authentication
                or the connection fails.
        """
        await self.login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the OneView session and the underlying connection pool."""
        await self.logout()

    async def discover_api_version(self) -> int:
        """
        Settle which `X-Api-Version` this run will send.

        Discovered from the unauthenticated `GET /rest/version` and clamped
        to `MAX_TESTED_API_VERSION` (docs/hpe-collectors.md, "API version").

        Returns:
            int: The version to send on every subsequent request.

        Raises:
            OneViewConnectionError: If the appliance is unreachable, or if
                a configured `INVENTORY_ONEVIEW_API_VERSION` falls outside
                the range it supports.
        """
        body = await self.get_json("/rest/version", authenticated=False)
        current = _as_int(body.get("currentVersion"))
        minimum = _as_int(body.get("minimumVersion"))
        if current is None:
            raise OneViewConnectionError(
                f"OneView at {self._endpoint} answered GET /rest/version without a "
                "currentVersion, so no X-Api-Version can be chosen."
            )
        floor = minimum if minimum is not None else current

        if self._configured_api_version:
            chosen = self._configured_api_version
            if not floor <= chosen <= current:
                raise OneViewConnectionError(
                    f"INVENTORY_ONEVIEW_API_VERSION={chosen} is outside the range "
                    f"{self._endpoint} supports ({floor}..{current}). Unset it to let "
                    "the collector discover the version itself."
                )
        elif floor > MAX_TESTED_API_VERSION:
            chosen = floor
            logger.warning(
                "oneview.api_version_above_tested",
                endpoint=self._endpoint,
                minimum=floor,
                tested=MAX_TESTED_API_VERSION,
                hint=(
                    "This appliance no longer supports the newest API version this "
                    "collector was written against, so its oldest supported version is "
                    "being used instead. Field names may have moved; verify with "
                    "`uv run python -m tools.verify_oneview`."
                ),
            )
        else:
            chosen = min(current, MAX_TESTED_API_VERSION)

        self._api_version = chosen
        logger.info(
            "oneview.api_version",
            endpoint=self._endpoint,
            chosen=chosen,
            appliance_current=current,
            appliance_minimum=minimum,
            tested=MAX_TESTED_API_VERSION,
        )
        return chosen

    async def login(self) -> None:
        """
        Discover the API version, then authenticate and hold the session.

        `sessionID` is replayed in an `Auth` header and `loginMsgAck` is
        always sent (docs/hpe-collectors.md, "Sessions").

        Raises:
            OneViewConnectionError: If the appliance is unreachable or
                rejects the credentials.
        """
        await self.discover_api_version()
        payload = {
            "userName": self._username,
            "password": self._password,
            "loginMsgAck": True,
        }
        body = await self._request_json("POST", "/rest/login-sessions", json=payload)
        session_id = body.get("sessionID")
        if not isinstance(session_id, str) or not session_id:
            raise OneViewConnectionError(
                f"OneView at {self._endpoint} accepted the login but returned no sessionID."
            )
        self._session_id = session_id
        self._http.headers["Auth"] = session_id
        logger.info("oneview.connected", endpoint=self._endpoint, api_version=self.api_version)

    async def logout(self) -> None:
        """
        Delete the session and close the connection pool, best-effort.

        A failed logout is logged, never raised, so it cannot mask the
        caller's own error.
        """
        try:
            if self._session_id is not None:
                await self._http.delete("/rest/login-sessions", headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("oneview.logout_failed", endpoint=self._endpoint, error=str(exc))
        finally:
            self._session_id = None
            self._http.headers.pop("Auth", None)
            await self._http.aclose()

    async def get_all(
        self,
        path: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch every member of a paged OneView collection.

        Follows `nextPageUri` with the SDK's two loop guards, and detects
        truncation against `total` (docs/hpe-collectors.md, "Pagination, and the 256 ceiling").

        Args:
            path (str): Collection path, e.g. `"/rest/server-hardware"`.
            page_size (int): Explicit `count`. Never `-1`.
            params (dict[str, str] | None): Extra query parameters, e.g.
                `{"expand": "all"}`.

        Returns:
            list[dict[str, Any]]: Every member fetched, in page order.

        Raises:
            OneViewConnectionError: On any non-2xx response or network
                failure.
        """
        query = {"start": "0", "count": str(page_size), **(params or {})}
        next_path: str | None = f"{path}?{urlencode(query)}"
        seen: set[str] = set()
        members: list[dict[str, Any]] = []
        total: int | None = None

        while next_path:
            seen.add(next_path)
            body = await self.get_json(next_path)
            page = body.get("members")
            if isinstance(page, list):
                members.extend(item for item in page if isinstance(item, dict))
            reported = _as_int(body.get("total"))
            if reported is not None:
                total = reported
            following = body.get("nextPageUri")
            if not isinstance(following, str) or not following:
                break
            if following == body.get("uri") or following in seen:
                logger.warning(
                    "oneview.self_referential_page",
                    endpoint=self._endpoint,
                    path=path,
                    next_page_uri=following,
                )
                break
            next_path = following

        if total is not None and len(members) < total:
            logger.error(
                "oneview.collection_truncated",
                endpoint=self._endpoint,
                path=path,
                fetched=len(members),
                reported_total=total,
                page_size=page_size,
                hint=(
                    "The appliance reports more members than paging returned. On "
                    "/rest/server-profiles this is the documented 256 ceiling: HPE says "
                    "the list is truncated without saying whether nextPageUri continues "
                    "past it. Servers beyond this point were NOT collected."
                ),
            )
            # Read into `collection_errors` — docs/hpe-collectors.md, "Pagination".
            self.truncations.append(
                f"{path}: appliance reports {total} member(s) but paging with "
                f"count={page_size} returned only {len(members)} — /rest/server-profiles "
                "has a documented 256-per-request ceiling"
            )
        return members

    async def raw_get(self, path: str, *, send_version: bool = True) -> httpx.Response:
        """
        Make one unwrapped GET and hand back the whole response.

        For `tools/verify_oneview.py`, which probes what an appliance does
        when `X-Api-Version` is omitted and needs the raw status and body.

        Args:
            path (str): Absolute appliance path.
            send_version (bool): Whether to send `X-Api-Version`. The
                session token is always sent, so `False` isolates the
                version header as the only variable.

        Returns:
            httpx.Response: The response, whatever its status.

        Raises:
            OneViewConnectionError: On a network-level failure only. A
                non-2xx response is returned, not raised — it is the
                answer the probe is looking for.
        """
        headers = self._headers()
        if not send_version:
            headers.pop("X-Api-Version", None)
        try:
            return await self._http.get(path, headers=headers)
        except httpx.HTTPError as exc:
            raise OneViewConnectionError(
                f"OneView GET {path} could not reach {self._endpoint}: {exc}."
            ) from exc

    def _headers(self) -> dict[str, str]:
        """
        The headers every authenticated request carries.

        Returns:
            dict[str, str]: `X-Api-Version`, plus `Auth` once a session
                exists.
        """
        headers = {"X-Api-Version": str(self.api_version)}
        if self._session_id:
            headers["Auth"] = self._session_id
        return headers

    async def get_json(self, path: str, *, authenticated: bool = True) -> dict[str, Any]:
        """
        GET one path and return its JSON object.

        Args:
            path (str): Absolute appliance path, e.g. `"/rest/version"`
                or `"/rest/server-hardware/{id}/powerSupplies"`.
            authenticated (bool): Whether to send `Auth`/`X-Api-Version`.
                `GET /rest/version` is the one operation documented as
                needing neither.

        Returns:
            dict[str, Any]: The decoded response body.

        Raises:
            OneViewConnectionError: On any non-2xx response or network
                failure.
        """
        return await self._request_json("GET", path, authenticated=authenticated)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """
        Make one request and return its JSON object.

        Args:
            method (str): HTTP method.
            path (str): Absolute appliance path.
            json (dict[str, Any] | None): Request body, if any.
            authenticated (bool): Whether to send the session headers.
                A login POST sends `X-Api-Version` but has no session
                yet, which `_headers` already expresses.

        Returns:
            dict[str, Any]: The decoded response body, or `{}` when the
                body is not a JSON object.

        Raises:
            OneViewConnectionError: On any non-2xx response or network
                failure.
        """
        headers = self._headers() if authenticated else {}
        try:
            response = await self._http.request(method, path, json=json, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OneViewConnectionError(
                f"OneView {method} {path} on {self._endpoint} failed ({exc.response.status_code})."
            ) from exc
        except httpx.HTTPError as exc:
            raise OneViewConnectionError(
                f"OneView {method} {path} could not reach {self._endpoint}: {exc}."
            ) from exc
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise OneViewConnectionError(
                f"OneView {method} {path} on {self._endpoint} did not return JSON."
            ) from exc
        return body if isinstance(body, dict) else {}


def _as_int(value: object) -> int | None:
    """
    Read an integer field, or `None` when it is absent or not numeric.

    Args:
        value (object): Any reported value.

    Returns:
        int | None: The integer, or `None`.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
