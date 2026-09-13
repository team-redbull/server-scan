"""Async wrapper over `ucscsdk`'s synchronous `UcscHandle`.

See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

import structlog
from ucscsdk.ucscexception import UcscError, UcscWrapperException
from ucscsdk.ucschandle import UcscHandle

from app.infrastructure.blocking import run_abandonable

logger = structlog.get_logger(__name__)


class UcsCentralConnectionError(Exception):
    """
    Any failure talking to UCS Central.

    Auth, XML API error, network, or the deadline this module imposes
    because the SDK has none. See docs/cisco-collectors.md, "SDK behaviour".
    """


def _validate_endpoint(endpoint: str) -> str:
    """
    Check that an endpoint is the bare host or IP `UcscHandle` requires.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".

    Args:
        endpoint (str): The configured UCS Central address.

    Returns:
        str: The endpoint, stripped of surrounding whitespace.

    Raises:
        ValueError: If the endpoint is empty, carries a URL scheme, or
            includes a port.
    """
    candidate = endpoint.strip()
    if not candidate:
        raise ValueError("UCS Central endpoint is empty.")
    if "://" in candidate:
        host = urlparse(candidate).hostname or ""
        raise ValueError(
            f"UCS Central endpoint {endpoint!r} must be a bare hostname or IP, not a URL — "
            f"ucscsdk builds the URL itself (use {host!r})."
        )
    if ":" in candidate and not candidate.startswith("["):  # not a bare IPv6 literal
        raise ValueError(
            f"UCS Central endpoint {endpoint!r} must not include a port — "
            "ucscsdk hardcodes 443 and rejects anything else."
        )
    return candidate


class UcsCentralClient:
    """
    One UCS Central session, held for the length of a single collector run.

    Not pooled or reused across runs.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
    """

    def __init__(
        self, *, endpoint: str, username: str, password: str, timeout_seconds: float
    ) -> None:
        """
        Build a handle for one UCS Central endpoint.

        Args:
            endpoint (str): Bare hostname or IP of the UCS Central instance.
            username (str): Login name.
            password (str): Login password.
            timeout_seconds (float): Deadline applied to each SDK call.

        Raises:
            ValueError: If `endpoint` is not a bare hostname or IP.
        """
        self._handle = UcscHandle(_validate_endpoint(endpoint), username, password)
        self._timeout_seconds = timeout_seconds
        self._poisoned_since: str | None = None
        if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
            self._handle.set_dump_xml()

    async def _with_timeout(self, func: Any, *args: Any, what: str) -> Any:
        """
        Run a blocking SDK call on an abandonable thread under a deadline.

        See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".

        Args:
            func (Any): The blocking SDK callable to dispatch.
            *args (Any): Positional arguments forwarded to `func`.
            what (str): Human-readable description of the call, used in
                error messages.

        Returns:
            Any: Whatever `func` returned.

        Raises:
            UcsCentralConnectionError: On timeout, any SDK exception, a
                network-level `OSError`, or if this client is already
                poisoned by an earlier abandoned call.
        """
        if self._poisoned_since is not None:
            raise UcsCentralConnectionError(
                f"{what} refused: {self._poisoned_since!r} never returned within its "
                "deadline, and ucscsdk gives no way to cancel it — its thread may still "
                "be running against this handle. Reusing the handle risks two threads "
                "touching the same ucscsdk session at once, so this client instance is "
                "done; build a fresh one."
            )
        try:
            async with asyncio.timeout(self._timeout_seconds) as deadline:
                return await run_abandonable(func, *args, name=f"ucs-central-{what}")
        except TimeoutError as exc:
            if deadline.expired():
                # Poisoned: the thread keeps running against the handle —
                # docs/cisco-collectors.md, "Timeouts, abandoned threads ...".
                self._poisoned_since = what
                raise UcsCentralConnectionError(
                    f"{what} timed out after {self._timeout_seconds}s "
                    "(ucscsdk has no timeout of its own; this deadline is imposed by "
                    "the collector, and its thread is abandoned, not stopped)."
                ) from exc
            # Unreached for `ucscsdk` (no timeout of its own); kept as a boundary.
            raise UcsCentralConnectionError(f"{what} failed: {exc}") from exc
        except (UcscError, UcscWrapperException) as exc:
            raise UcsCentralConnectionError(f"{what} failed: {exc}") from exc
        except OSError as exc:
            raise UcsCentralConnectionError(
                f"{what} could not reach UCS Central at {self._handle.ip}: {exc}"
            ) from exc

    async def login(self) -> None:
        """
        Open the UCS Central session.

        Raises:
            UcsCentralConnectionError: If authentication or the connection
                fails.
        """
        await self._with_timeout(self._handle.login, what=f"Login to {self._handle.ip}")

    async def logout(self) -> None:
        """
        Close the UCS Central session, best-effort.

        Never raises, so it is safe in a `finally`; before a successful
        login it is a no-op. See docs/cisco-collectors.md, "SDK behaviour".
        """
        try:
            await self._with_timeout(self._handle.logout, what="Logout")
        except Exception as exc:
            logger.warning("ucs_central.logout_failed", endpoint=self._handle.ip, error=str(exc))

    async def query_classid(self, class_id: str) -> list[Any]:
        """
        Resolve every instance of `class_id` across every registered domain.

        See docs/cisco-collectors.md, "UCS Central domain discovery and
        pruning" for why no server-side `filter_str` is used.

        Args:
            class_id (str): The managed-object class to resolve, e.g.
                `"computeSystem"`.

        Returns:
            list[Any]: Every matching managed object, empty if none.

        Raises:
            UcsCentralConnectionError: On timeout, SDK error, or network
                failure.
        """
        # ponytail: one filter, applied once, in the collector. Push the
        # pattern down to filter_str only if payload size ever actually
        # hurts — at 10k servers this is a few MB per run.
        result = await self._with_timeout(
            self._handle.query_classid, class_id, what=f"query_classid({class_id!r})"
        )
        return list(result) if result else []
