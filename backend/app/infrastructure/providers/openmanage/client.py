"""Async HTTP client for a Dell OpenManage Enterprise (OME) appliance.

One OME appliance manages the whole Dell estate and answers a REST API
(`https://<appliance>/api/...`, OData-shaped). This client owns the
session lifecycle and the paging/timeout concerns; `mapping.py` turns the
JSON it returns into `ProviderServer` DTOs and `provider.py` orchestrates.

Async on `httpx.AsyncClient` rather than the synchronous `requests`
session the original production scanner used: the collector fans a
per-device inventory call out across the fleet, and native async lets that
run under a bounded `asyncio.Semaphore` instead of the sleep-batched
throttling that a blocking client forces. See docs/dell-collectors.md,
"OME REST surface".
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class OmeConnectionError(Exception):
    """
    Any failure talking to an OME appliance.

    Rejected credentials, a non-2xx response, or an unreachable appliance
    — one exception type per vendor.
    """


class OmeClient:
    """
    One authenticated OME session, held for a single collector run.

    Use as an async context manager. See docs/dell-collectors.md, "Session lifecycle".
    """

    def __init__(
        self,
        *,
        endpoint: str,
        username: str,
        password: str,
        timeout_seconds: float,
        verify_tls: bool = False,
    ) -> None:
        """
        Build a client for one OME appliance.

        Args:
            endpoint (str): Bare hostname or IP of the OME appliance.
            username (str): OME account name.
            password (str): OME account password.
            timeout_seconds (float): Per-request timeout.
            verify_tls (bool): Whether to verify the appliance's TLS
                certificate. Defaults to `False`: OME appliances in an
                air-gapped estate ship a self-signed certificate with no
                private CA to trust it against, so verification would fail
                every connection. Set `True` where a trusted chain exists.

        Raises:
            ValueError: If `endpoint` is empty.
        """
        host = endpoint.strip()
        if not host:
            raise ValueError("OME endpoint is empty.")
        self._endpoint = host
        self._username = username
        self._password = password
        self._session_id: str | None = None
        self._http = httpx.AsyncClient(
            base_url=f"https://{host}/api",
            timeout=timeout_seconds,
            verify=verify_tls,
        )

    async def __aenter__(self) -> OmeClient:
        """
        Open the OME session.

        Returns:
            OmeClient: This client, logged in and ready to query.

        Raises:
            OmeConnectionError: If authentication or the connection fails.
        """
        await self.login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the OME session and the underlying connection pool."""
        await self.logout()

    async def login(self) -> None:
        """
        Authenticate and capture the `X-Auth-Token` header and session `Id`.

        See docs/dell-collectors.md, "OME REST surface".

        Raises:
            OmeConnectionError: If the appliance is unreachable, rejects the
                credentials, or returns no token.
        """
        payload = {
            "UserName": self._username,
            "Password": self._password,
            "SessionType": "API",
        }
        try:
            response = await self._http.post("/SessionService/Sessions", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OmeConnectionError(
                f"OME at {self._endpoint} rejected login ({exc.response.status_code})."
            ) from exc
        except httpx.HTTPError as exc:
            raise OmeConnectionError(f"OME at {self._endpoint} is unreachable: {exc}.") from exc

        token = response.headers.get("X-Auth-Token")
        if not token:
            raise OmeConnectionError(
                f"OME at {self._endpoint} accepted the login but returned no X-Auth-Token."
            )
        self._http.headers["X-Auth-Token"] = token
        body = response.json()
        self._session_id = str(body.get("Id")) if body.get("Id") is not None else None
        logger.info("ome.connected", endpoint=self._endpoint)

    async def logout(self) -> None:
        """
        Delete the OME session and close the connection pool, best-effort.

        A failed logout is logged, never raised, so it cannot mask the
        caller's own error.
        """
        try:
            if self._session_id is not None:
                await self._http.delete(f"/SessionService/Sessions('{self._session_id}')")
        except httpx.HTTPError as exc:
            logger.warning("ome.logout_failed", endpoint=self._endpoint, error=str(exc))
        finally:
            self._session_id = None
            await self._http.aclose()

    async def get_all(self, path: str) -> list[dict[str, Any]]:
        """
        Fetch every item of a paged OME collection by following `@odata.nextLink`.

        See docs/dell-collectors.md, "OME REST surface".

        Args:
            path (str): Collection path relative to `/api`, e.g.
                `"/ProfileService/Profiles"`.

        Returns:
            list[dict[str, Any]]: Every item across all pages, in order.

        Raises:
            OmeConnectionError: On any non-2xx response or network failure.
        """
        items: list[dict[str, Any]] = []
        next_path: str | None = path
        while next_path:
            body = await self._get_json(next_path)
            page = body.get("value")
            if isinstance(page, list):
                items.extend(item for item in page if isinstance(item, dict))
            link = body.get("@odata.nextLink")
            next_path = _relative_path(link) if isinstance(link, str) and link else None
        return items

    async def _get_json(self, path: str) -> dict[str, Any]:
        """
        GET one path and return its JSON object.

        Args:
            path (str): Path relative to `/api`.

        Returns:
            dict[str, Any]: The decoded response body.

        Raises:
            OmeConnectionError: On any non-2xx response or network failure.
        """
        try:
            response = await self._http.get(path)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OmeConnectionError(
                f"OME GET {path} failed ({exc.response.status_code})."
            ) from exc
        except httpx.HTTPError as exc:
            raise OmeConnectionError(
                f"OME GET {path} could not reach {self._endpoint}: {exc}."
            ) from exc
        body = response.json()
        return body if isinstance(body, dict) else {}


def _relative_path(link: str) -> str:
    """
    Strip the leading `/api` from an OME `@odata.nextLink`.

    Args:
        link (str): An absolute OME link such as
            `"/api/ProfileService/Profiles?$skip=100&$top=100"`.

    Returns:
        str: The same link relative to the client's `/api` base_url.
    """
    return link.removeprefix("/api")
