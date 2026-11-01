"""The REST API as the MCP tools see it: one authenticated GET, errors made readable."""

from __future__ import annotations

from typing import Any

import httpx

_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

_HINTS = {
    401: "The token is missing, wrong or expired. Create a new one on the 'Claude' page.",
    403: "This token's role may not read that (the audit trail needs the auditor role).",
}


class ApiError(Exception):
    """A request the API refused or could not answer, worded for the model and the user."""


class ScanApi:
    """GET-only client for the Server Scan API."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        """
        Build the client.

        Args:
            base_url (str): The API's address, e.g. `http://backend:8080`.
            client (httpx.AsyncClient | None): A client to use instead of a fresh one (tests).
        """
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)

    async def get(self, path: str, token: str, params: dict[str, Any] | None = None) -> Any:
        """
        Read one API path as the token's owner.

        Args:
            path (str): Path under the base URL, e.g. `/api/v1/sites`.
            token (str): The caller's bearer token, passed through and never stored.
            params (dict[str, Any] | None): Query parameters; `None` values are dropped.

        Returns:
            Any: The decoded JSON body.

        Raises:
            ApiError: The API refused (401, 403, 4xx with its own `detail`), or was unreachable.
        """
        query = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=query,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"Server Scan did not answer ({type(exc).__name__}).") from exc
        if response.status_code == 200:
            return response.json()
        if response.status_code in _HINTS:
            raise ApiError(_HINTS[response.status_code])
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            detail = ""
        raise ApiError(f"Server Scan answered {response.status_code}. {detail}".strip())
