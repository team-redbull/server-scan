"""Unauthenticated BMC reachability probe and its provider wrapper (ADR-0037, decision 3)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ssl
from collections.abc import AsyncGenerator
from types import TracebackType

import httpx
import structlog

from app.domain.enums import UnreachableReason
from app.domain.ports.provider import (
    SERVER_FAILED_MARKER,
    ProviderServer,
    ServerIdentity,
    ServerInventoryProvider,
)
from app.domain.value_objects.bmc_address import parse_bmc_address

logger = structlog.get_logger(__name__)

PROBE_CONCURRENCY = 64
PROBE_TIMEOUT_SECONDS = 5.0
BMC_PORT = 443


class BmcProbe:
    """One shared client probing `GET /redfish/v1` per BMC; nothing in a response is trusted."""

    def __init__(
        self,
        *,
        concurrency: int = PROBE_CONCURRENCY,
        timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Build the shared client and concurrency bound.

        Args:
            concurrency (int): Max probes in flight.
            timeout_seconds (float): Connect/read/pool timeout per probe.
            transport (httpx.AsyncBaseTransport | None): Test seam; real transport when omitted.
        """
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            verify=False,  # noqa: S501 - no credential is sent, no response body is trusted
            timeout=timeout_seconds,
            limits=httpx.Limits(max_connections=concurrency),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> BmcProbe:
        """
        Enter the client's context.

        Returns:
            BmcProbe: This probe.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """
        Close the shared client.

        Args:
            exc_type (type[BaseException] | None): Unused.
            exc (BaseException | None): Unused.
            tb (TracebackType | None): Unused.
        """
        await self._client.aclose()

    async def probe(self, host: str, port: int = BMC_PORT) -> tuple[bool, UnreachableReason | None]:
        """
        Probe one BMC; classifies every `Exception`, never raises one.

        Args:
            host (str): Bare host or IP (IPv6 without brackets).
            port (int): HTTPS port.

        Returns:
            tuple[bool, UnreachableReason | None]: `(True, None)` when anything answered
                (any status, or a TLS failure), else `(False, reason)`.
        """
        netloc = f"[{host}]" if ":" in host else host
        async with self._semaphore:
            try:
                await self._client.get(f"https://{netloc}:{port}/redfish/v1")
            except httpx.InvalidURL as exc:
                logger.warning(
                    "bmc_probe.invalid_host",
                    host=host,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return False, UnreachableReason.COLLECTION_ERROR
            except httpx.TimeoutException:
                return False, UnreachableReason.TIMEOUT
            except httpx.ConnectError as exc:
                if _is_tls_failure(exc):
                    return True, None
                return False, UnreachableReason.NETWORK_UNREACHABLE
            except OSError:
                return False, UnreachableReason.NETWORK_UNREACHABLE
            except httpx.HTTPError:
                return True, None  # the peer spoke (protocol error, reset mid-response)
            except Exception as exc:
                logger.exception(
                    "bmc_probe.unexpected_error",
                    host=host,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return False, UnreachableReason.COLLECTION_ERROR
        return True, None


def _is_tls_failure(exc: BaseException) -> bool:
    """
    Report whether a connect error was a TLS handshake failure.

    Args:
        exc (BaseException): The httpx error.

    Returns:
        bool: True when an `ssl.SSLError` is in the cause/context chain.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class BmcProbedProvider(ServerInventoryProvider):
    """Stamp `reachable`/`unreachable_reason` on every server the wrapped collector yields."""

    def __init__(
        self, inner: ServerInventoryProvider, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """
        Wrap `inner`.

        Args:
            inner (ServerInventoryProvider): The real collector.
            transport (httpx.AsyncBaseTransport | None): Test seam for the probe client.
        """
        super().__init__()
        self._inner = inner
        self._transport = transport
        self.provider_type = inner.provider_type

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """The wrapped collector's partial failures plus this wrapper's own probe failures."""
        return (*self._inner.collection_errors, *self._collection_errors)

    @property
    def servers_seen(self) -> int:
        """Pass the wrapped collector's server count through."""
        return self._inner.servers_seen

    @property
    def server_failures(self) -> int:
        """The wrapped collector's per-server failures plus the probe steps that failed here."""
        return self._inner.server_failures + self._server_failures

    async def health_check(self) -> None:
        """Delegate to the wrapped collector."""
        await self._inner.health_check()

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Fetch one server from the wrapped collector and probe its BMC.

        Args:
            identity (ServerIdentity): Which server.

        Returns:
            ProviderServer | None: The server with probe result, or None if not found.
        """
        server = await self._inner.get_one(identity)
        if server is None:
            return None
        async with BmcProbe(transport=self._transport) as probe:
            return await self._stamp(probe, server)

    def _record_probe_failure(self, server: ProviderServer, cause: str) -> None:
        """
        Count and record one server whose BMC probe step could not run.

        Args:
            server (ProviderServer): The server that was being probed.
            cause (str): Why, in words.
        """
        self._server_failures += 1
        self._record_error(
            f"{server.name} (id {server.external_id}, serial {server.serial})"
            f"{SERVER_FAILED_MARKER}probing its BMC: {cause}"
        )

    async def _stamp(self, probe: BmcProbe, server: ProviderServer) -> ProviderServer:
        """
        Probe one server's BMC host on 443; a failure here marks only this server.

        Args:
            probe (BmcProbe): The shared probe.
            server (ProviderServer): A fully-read server.

        Returns:
            ProviderServer: A copy with the probe result, or `server` itself without a BMC
                host or when the collector already marked it unreachable.
        """
        if not server.reachable:
            return server
        try:
            address = parse_bmc_address(server.bmc_address_raw)
            if address is None or not address.host:
                return server
            reachable, reason = await probe.probe(address.host)
        except Exception as exc:
            logger.exception(
                "bmc_probe.server_failed",
                host=server.bmc_address_raw,
                server=server.name,
                serial=server.serial,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            cause = f"{type(exc).__name__}: {exc}"
            self._record_probe_failure(server, cause)
            return dataclasses.replace(
                server,
                reachable=False,
                unreachable_reason=UnreachableReason.COLLECTION_ERROR,
                unreachable_detail=f"the BMC probe could not run: {cause}",
            )
        detail = None
        if reason is UnreachableReason.COLLECTION_ERROR:
            detail = (
                f"the BMC probe could not run for {address.host!r}: invalid host or probe error "
                "(see the bmc_probe log)"
            )
            self._record_probe_failure(server, detail)
        return dataclasses.replace(
            server, reachable=reachable, unreachable_reason=reason, unreachable_detail=detail
        )

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield the wrapped collector's servers, probed in bounded batches, in order.

        Yields:
            ProviderServer: Each inner server with its probe result.
        """
        async with (
            BmcProbe(transport=self._transport) as probe,
            contextlib.aclosing(self._inner.collect()) as servers,
        ):
            batch: list[ProviderServer] = []
            try:
                async for server in servers:
                    batch.append(server)
                    if len(batch) >= PROBE_CONCURRENCY:
                        for done in await asyncio.gather(*(self._stamp(probe, s) for s in batch)):
                            yield done
                        batch = []
            except Exception:
                # Already-mapped servers of this batch must not die with the inner failure.
                for done in await asyncio.gather(*(self._stamp(probe, s) for s in batch)):
                    yield done
                raise
            for done in await asyncio.gather(*(self._stamp(probe, s) for s in batch)):
                yield done
