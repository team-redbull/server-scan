"""Async wrapper over `ucsmsdk`'s synchronous `UcsHandle`.

See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

import structlog
from ucsmsdk.ucsexception import UcsError, UcsWrapperException
from ucsmsdk.ucshandle import UcsHandle

from app.infrastructure.blocking import run_abandonable

logger = structlog.get_logger(__name__)


class UcsManagerConnectionError(Exception):
    """
    Any failure talking to a UCS Manager domain.

    Credentials, XML API error, or network. See docs/cisco-collectors.md,
    "SDK behaviour, sessions and timeouts".
    """


def _validate_endpoint(endpoint: str) -> str:
    """
    Check that an endpoint is the bare host or IP `UcsHandle` expects.

    Args:
        endpoint (str): Hostname or IP address, without scheme or port.

    Returns:
        str: The endpoint, stripped of surrounding whitespace.

    Raises:
        ValueError: If the endpoint is empty, carries a URL scheme, or
            includes a port.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
    """
    candidate = endpoint.strip()
    if not candidate:
        raise ValueError("UCS Manager endpoint is empty.")
    if "://" in candidate:
        host = urlparse(candidate).hostname or ""
        raise ValueError(
            f"UCS Manager endpoint {endpoint!r} must be a bare hostname or IP, not a URL — "
            f"ucsmsdk builds the URL itself (use {host!r})."
        )
    if ":" in candidate and not candidate.startswith("["):  # not a bare IPv6 literal
        raise ValueError(
            f"UCS Manager endpoint {endpoint!r} must not include a port — "
            "ucsmsdk appends one itself (443 by default)."
        )
    return candidate


class UcsManagerClient:
    """
    One UCS Manager session, for one domain, for one collector run.

    Not pooled or reused across domains.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
    """

    def __init__(
        self, *, endpoint: str, username: str, password: str, timeout_seconds: float
    ) -> None:
        """
        Build a client bound to one UCS Manager domain.

        Args:
            endpoint (str): Bare hostname or IP of the domain.
            username (str): Login user.
            password (str): Login password.
            timeout_seconds (float): Also imposed as a whole-call deadline
                by `_with_timeout` — see its docstring for why the
                per-socket-operation timeout `UcsHandle` itself takes is
                not enough on its own.

        Raises:
            ValueError: If `endpoint` is not a bare hostname or IP.
        """
        self._handle = UcsHandle(
            _validate_endpoint(endpoint), username, password, timeout=timeout_seconds
        )
        self._timeout_seconds = timeout_seconds
        self._poisoned_since: str | None = None
        if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
            self._handle.set_dump_xml()

    async def _with_timeout(self, func: Any, *args: Any, what: str) -> Any:
        """
        Run a blocking SDK call on an abandonable thread under a deadline.

        `UcsHandle(timeout=...)` bounds one socket operation, not the call —
        see docs/cisco-collectors.md, "`ucsmsdk` (UCS Manager)".

        Args:
            func (Any): The blocking SDK callable to dispatch.
            *args (Any): Positional arguments forwarded to `func`.
            what (str): Human-readable description of the call, used in
                error messages.

        Returns:
            Any: Whatever `func` returned.

        Raises:
            UcsManagerConnectionError: On timeout, any SDK exception, a
                network-level `OSError`, or if this client is already
                poisoned by an earlier abandoned call.
        """
        if self._poisoned_since is not None:
            raise UcsManagerConnectionError(
                f"{what} refused: {self._poisoned_since!r} never returned within its "
                "deadline, and ucsmsdk gives no way to cancel it — its thread may still "
                "be running against this handle. Reusing the handle risks two threads "
                "touching the same ucsmsdk session at once, so this client instance is "
                "done; build a fresh one."
            )
        try:
            async with asyncio.timeout(self._timeout_seconds) as deadline:
                return await run_abandonable(func, *args, name=f"ucs-manager-{what}")
        except TimeoutError as exc:
            if deadline.expired():
                # Poisoned: the thread keeps running against the handle —
                # docs/cisco-collectors.md, "Timeouts, abandoned threads ...".
                self._poisoned_since = what
                raise UcsManagerConnectionError(
                    f"{what} timed out after {self._timeout_seconds}s "
                    "(the socket-level timeout UcsHandle was built with does not bound the "
                    "whole call; this deadline is imposed by the collector, and its thread "
                    "is abandoned, not stopped)."
                ) from exc
            # The SDK's own socket timeout, thread already finished — not our deadline.
            raise UcsManagerConnectionError(
                f"{what} could not reach {self._handle.ip}: {exc}"
            ) from exc
        except (UcsError, UcsWrapperException) as exc:
            raise UcsManagerConnectionError(f"{what} failed: {exc}") from exc
        except OSError as exc:
            raise UcsManagerConnectionError(
                f"{what} could not reach {self._handle.ip}: {exc}"
            ) from exc

    async def login(self) -> None:
        """
        Open a session against the domain.

        Raises:
            UcsManagerConnectionError: If the credentials are rejected, the
                XML API returns an error, the host is unreachable, or the
                call exceeded its deadline.
        """
        await self._with_timeout(self._handle.login, what="login")

    async def logout(self) -> None:
        """
        Close the session, best-effort.

        Never raises; before a successful login it is a no-op. See
        docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
        """
        try:
            await self._with_timeout(self._handle.logout, what="logout")
        except Exception as exc:
            logger.warning("ucs_manager.logout_failed", endpoint=self._handle.ip, error=str(exc))

    async def query_classid(self, class_id: str) -> list[Any]:
        """
        Resolve every instance of one MO class in the whole domain.

        Args:
            class_id (str): MO class name, e.g. "computeBlade".

        Returns:
            list[Any]: The matching `ucsmsdk` MOs, empty when none match.

        Raises:
            UcsManagerConnectionError: On an XML API error, a network
                failure, or the call exceeding its deadline.

        See docs/cisco-collectors.md, "Shared object model and DN joins".
        """
        result = await self._with_timeout(
            self._handle.query_classid, class_id, what=f"query_classid({class_id!r})"
        )
        return list(result) if result else []

    async def query_dn(self, dn: str, *, hierarchy: bool = False) -> list[Any]:
        """
        Resolve one managed object by DN, optionally with its whole subtree (ADR-0032).

        `hierarchy=True` is `ucsmsdk`'s own documented behaviour: one round
        trip returns the object and every descendant MO as a flat list.

        Args:
            dn (str): The object's distinguished name.
            hierarchy (bool): Whether to also fetch every descendant MO.

        Returns:
            list[Any]: `[]` when `dn` no longer resolves; the object alone
                (`hierarchy=False`) or the object plus every descendant
                (`hierarchy=True`) otherwise.

        Raises:
            UcsManagerConnectionError: On an XML API error, a network
                failure, or the call exceeding its deadline.
        """
        result = await self._with_timeout(
            self._handle.query_dn, dn, hierarchy, what=f"query_dn({dn!r}, hierarchy={hierarchy})"
        )
        if result is None:
            return []
        return list(result) if hierarchy else [result]
