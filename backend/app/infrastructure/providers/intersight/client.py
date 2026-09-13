"""HTTP transport for the Cisco Intersight REST API.

Paged OData list queries over `httpx`, signed per request by
`.signing`. Deliberately not the official `intersight` SDK — see
docs/adr/0017-intersight-collector.md, "Decision 1", which also records
that the SDK has no retry or backoff for HTTP 429 and that Cisco
publishes no rate-limit numbers, so throttling is handled here.

Nothing in this module logs a header, a body or a key. The one tracing
hook (`debug_http`) logs method, path and status only, following the
Redfish collector's precedent.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import AsyncIterator, Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import structlog

from app.infrastructure.providers.intersight.signing import (
    IntersightKeyError,
    IntersightSigner,
)

logger = structlog.get_logger(__name__)

_API_ROOT = "/api/v1"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_MAX_CLOCK_SKEW_SECONDS = 60.0


class IntersightError(Exception):
    """Any failure talking to Intersight."""


class IntersightUnreachableError(IntersightError):
    """The endpoint did not answer: DNS, TCP, TLS or timeout."""


class IntersightAuthError(IntersightError):
    """Intersight rejected the signature (HTTP 401)."""


class IntersightForbiddenError(IntersightError):
    """Authenticated, but this API key's role may not read this (HTTP 403)."""


class IntersightProtocolError(IntersightError):
    """A response that was not the JSON list document the API documents."""


class IntersightThrottledError(IntersightError):
    """Still throttled after the retry budget was spent."""


def validate_endpoint(endpoint: str) -> str:
    """
    Check that a configured endpoint is the bare host this client needs.

    The one place a host becomes a URL: the signed `Host` header and the
    `httpx` base URL are both derived from the return value.

    Args:
        endpoint (str): `INVENTORY_INTERSIGHT_IP` as configured —
            `intersight.com`, or an on-prem appliance's FQDN.

    Returns:
        str: The lowercased bare host.

    Raises:
        ValueError: If it is empty, carries a scheme or path, or
            includes a port.
    """
    candidate = endpoint.strip().lower()
    if not candidate:
        raise ValueError("Intersight endpoint is empty.")
    if "://" in candidate or "/" in candidate:
        host = urlparse(candidate).hostname or candidate.split("/")[0]
        raise ValueError(
            f"Intersight endpoint {endpoint!r} must be a bare hostname, not a URL — "
            f"the collector builds the URL itself (use {host!r})."
        )
    if ":" in candidate and not candidate.startswith("["):
        raise ValueError(
            f"Intersight endpoint {endpoint!r} must not include a port — the API is "
            "served over HTTPS on 443."
        )
    return candidate


class IntersightClient:
    """
    One signed, paged conversation with an Intersight endpoint.

    **Never verifies the endpoint's TLS certificate**, unconditionally and
    by explicit user decision — ADR-0017, "Update (2026-08-31)".
    """

    def __init__(
        self,
        *,
        endpoint: str,
        key_id: str,
        private_key_pem: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        page_size: int = 1000,
        max_retries: int = 4,
        debug_http: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build a client configured for one Intersight appliance and API key.

        Args:
            endpoint (str): Bare host — `intersight.com`, or the
                appliance FQDN for a Private Virtual Appliance.
            key_id (str): The API Key ID.
            private_key_pem (str): The key's unencrypted PEM private half.
            connect_timeout (float): Seconds to establish a connection.
            read_timeout (float): Seconds to wait for one response. Well
                above the connect timeout because a fleet-wide list query
                at `$top=1000` is a large response.
            page_size (int): `$top`, capped at the API's documented 1000.
            max_retries (int): Attempts per request after a retryable
                status, beyond the first.
            debug_http (bool): Log method, path and status per request.
                Never headers or bodies.
            transport (httpx.AsyncBaseTransport | None): Injected in
                tests; production passes nothing.

        Raises:
            ValueError: If the endpoint is not a bare host.
            IntersightKeyError: If the API key cannot be used.
        """
        self._host = validate_endpoint(endpoint)
        self._signer = IntersightSigner(key_id=key_id, private_key_pem=private_key_pem)
        self._page_size = max(1, min(page_size, 1000))
        self._max_retries = max(0, max_retries)
        self._debug_http = debug_http
        self._client = httpx.AsyncClient(
            base_url=f"https://{self._host}",
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            verify=False,  # noqa: S501 - unconditional, see class docstring
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> IntersightClient:
        """Enter the async context manager.

        Returns:
            IntersightClient: This client.
        """
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the client on the way out of the block."""
        await self.aclose()

    async def health_check(self) -> None:
        """
        Prove the endpoint answers and the API key is accepted.

        One row of the cheapest inventory resource, so the probe exercises
        the same signing path collection depends on.

        Raises:
            IntersightUnreachableError: If the endpoint did not answer.
            IntersightAuthError: If the signature was rejected.
            IntersightForbiddenError: If the key's role may not read
                inventory.
        """
        await self._request("compute/PhysicalSummaries", {"$top": "1"})

    async def list_all(
        self,
        resource: str,
        *,
        select: str | None = None,
        filter_expr: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Every managed object of one resource, a page at a time.

        `$top`/`$skip` ordered by `Moid` — see docs/cisco-collectors.md,
        "Transport", and ADR-0017, "Decision 6".

        Args:
            resource (str): Path under `/api/v1`, e.g.
                `"compute/PhysicalSummaries"`.
            select (str | None): `$select` field list. Always passed in
                practice — it is what keeps the fleet-wide join tables
                small enough to hold in memory.
            filter_expr (str | None): `$filter` expression.

        Yields:
            dict[str, Any]: One managed object.

        Raises:
            IntersightError: On any failure this run cannot continue past.
        """
        skip = 0
        while True:
            params: dict[str, str] = {
                "$top": str(self._page_size),
                "$skip": str(skip),
                "$orderby": "Moid",
            }
            if select:
                params["$select"] = select
            if filter_expr:
                params["$filter"] = filter_expr

            payload = await self._request(resource, params)
            results = payload.get("Results")
            rows = list(results) if isinstance(results, list) else []
            for row in rows:
                if isinstance(row, dict):
                    yield row
            if len(rows) < self._page_size:
                return
            skip += self._page_size

    async def _request(self, resource: str, params: Mapping[str, str]) -> dict[str, Any]:
        """
        One signed GET, with retries for the statuses worth retrying.

        Args:
            resource (str): Path under `/api/v1`.
            params (Mapping[str, str]): Query parameters.

        Returns:
            dict[str, Any]: The decoded response document.

        Raises:
            IntersightError: On a failure the retry budget cannot absorb.
        """
        path = f"{_API_ROOT}/{resource.lstrip('/')}"
        # Encoded once and signed verbatim: httpx could encode `$` differently.
        query = urlencode(params)
        url = f"{path}?{query}"

        last_status: int | None = None
        for attempt in range(self._max_retries + 1):
            signed = self._signer.sign(
                method="GET", path=path, query=query, host=self._host, now=time.time()
            )
            try:
                response = await self._client.get(
                    url, headers={**signed.headers, "Accept": "application/json"}
                )
            except httpx.TimeoutException as exc:
                raise IntersightUnreachableError(
                    f"Intersight at {self._host} timed out on {resource}: {exc}"
                ) from exc
            except httpx.TransportError as exc:
                raise IntersightUnreachableError(
                    f"Intersight at {self._host} is unreachable: {exc}"
                ) from exc

            if self._debug_http:
                logger.info("intersight.http", method="GET", path=path, status=response.status_code)

            if response.status_code == 200:
                return self._decode(response, resource)
            last_status = response.status_code

            if response.status_code == 401:
                raise IntersightAuthError(self._auth_message(response))
            if response.status_code == 403:
                raise IntersightForbiddenError(
                    f"Intersight rejected the API key's permissions reading {resource} "
                    "(HTTP 403). The key's role needs read access to server inventory — "
                    "a Read-Only role is enough." + self._api_error(response)
                )
            if response.status_code not in _RETRY_STATUSES:
                raise IntersightProtocolError(
                    f"Intersight returned HTTP {response.status_code} for {resource}."
                    + self._api_error(response)
                )
            if attempt == self._max_retries:
                break
            await asyncio.sleep(self._backoff_seconds(response, attempt))

        if last_status == 429:
            raise IntersightThrottledError(
                f"Intersight is still throttling after {self._max_retries + 1} attempts at "
                f"{resource}. Cisco publishes no rate limit, so lower "
                "INVENTORY_INTERSIGHT_PAGE_SIZE or run the CronJob less often."
            )
        raise IntersightError(
            f"Intersight returned HTTP {last_status} for {resource} after "
            f"{self._max_retries + 1} attempts."
        )

    @staticmethod
    def _api_error(response: httpx.Response) -> str:
        """
        Intersight's own description of a failure, if it sent one.

        Error-body shape confirmed live 2026-08-29 — see
        docs/cisco-collectors.md, "Transport".

        Args:
            response (httpx.Response): The failed response.

        Returns:
            str: A parenthesised summary, or an empty string when the body
                was not the documented shape.
        """
        try:
            body = response.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        message = str(body.get("message") or "").strip()
        trace = str(body.get("traceId") or "").strip()
        if not message:
            return ""
        return f' Intersight said: "{message}"' + (f" (traceId {trace})" if trace else "")

    @staticmethod
    def _message_id(response: httpx.Response) -> str:
        """
        Intersight's own machine-readable reason for a failure.

        Args:
            response (httpx.Response): The failed response.

        Returns:
            str: The `messageId`, or `""` when the body had none.
        """
        try:
            body = response.json()
        except ValueError:
            return ""
        return str(body.get("messageId") or "") if isinstance(body, dict) else ""

    def _auth_message(self, response: httpx.Response) -> str:
        """
        Explain a 401 as far as a 401 can be explained.

        Branches on `messageId`, then on this host's clock skew — see
        docs/cisco-collectors.md, "Transport", for the live-confirmed ids.

        Args:
            response (httpx.Response): The rejected response.

        Returns:
            str: The message for the raised error.
        """
        message_id = self._message_id(response)
        if message_id == "iam_apikey_signature_invalid":
            return (
                "Intersight could not parse this request's Authorization header "
                "(HTTP 401, iam_apikey_signature_invalid). That is a fault in the "
                "collector's request signing, not in your API key — the key and its id "
                "are very likely fine. Please report it with the traceId below."
                + self._api_error(response)
            )
        if message_id == "iam_cookie_invalid":
            return (
                "Intersight received no API key credentials at all (HTTP 401, "
                "iam_cookie_invalid) — it fell back to looking for a session cookie. "
                "Something stripped the Authorization header in transit, such as a proxy."
                + self._api_error(response)
            )

        skew = self._clock_skew_seconds(response)
        if skew is not None and abs(skew) > _MAX_CLOCK_SKEW_SECONDS:
            return (
                f"Intersight rejected the request signature (HTTP 401), and this host's "
                f"clock is {skew:+.0f}s from Intersight's. A signature is only valid "
                "briefly, so fix the clock (NTP on the node) before suspecting the key."
                + self._api_error(response)
            )
        return (
            "Intersight rejected the request signature (HTTP 401). Intersight answers an "
            "expired key, a revoked key, a wrong API Key ID and a key that does not match "
            "that id identically, so check in that order: that "
            "INVENTORY_INTERSIGHT_API_KEY_ID is the API Key ID shown in Settings > API "
            "Keys, that INVENTORY_INTERSIGHT_API_KEY_PEM is that same key's private PEM, "
            "and that the key is still listed and unexpired." + self._api_error(response)
        )

    @staticmethod
    def _clock_skew_seconds(response: httpx.Response) -> float | None:
        """
        This host's clock minus the API's, in seconds.

        Args:
            response (httpx.Response): Any response carrying a `Date`.

        Returns:
            float | None: The difference, or None if there was no usable
                `Date` header.
        """
        raw = response.headers.get("Date")
        if not raw:
            return None
        try:
            served = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return float(time.time() - served.timestamp())

    def _backoff_seconds(self, response: httpx.Response, attempt: int) -> float:
        """
        How long to wait before retrying.

        `Retry-After` when sent, else exponential with full jitter so
        collectors sharing a tenant do not retry in lockstep.

        Args:
            response (httpx.Response): The retryable response.
            attempt (int): Zero-based attempt already made.

        Returns:
            float: Seconds to sleep, capped at 60.
        """
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(60.0, random.uniform(0.0, math.pow(2.0, attempt)))  # noqa: S311

    @staticmethod
    def _decode(response: httpx.Response, resource: str) -> dict[str, Any]:
        """
        Decode a 200 into the list document the API documents.

        Args:
            response (httpx.Response): The successful response.
            resource (str): Resource path, for the error message.

        Returns:
            dict[str, Any]: The decoded document.

        Raises:
            IntersightProtocolError: If it was not a JSON object.
        """
        try:
            payload = response.json()
        except ValueError as exc:
            raise IntersightProtocolError(
                f"Intersight returned a non-JSON body for {resource}."
            ) from exc
        if not isinstance(payload, dict):
            raise IntersightProtocolError(
                f"Intersight returned {type(payload).__name__}, not an object, for {resource}."
            )
        return payload


__all__ = [
    "IntersightAuthError",
    "IntersightClient",
    "IntersightError",
    "IntersightForbiddenError",
    "IntersightKeyError",
    "IntersightProtocolError",
    "IntersightThrottledError",
    "IntersightUnreachableError",
    "validate_endpoint",
]
