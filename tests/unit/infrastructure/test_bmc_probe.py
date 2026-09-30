"""BMC probe classification and the OneView wrapper (ADR-0037, decision 3)."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator, Callable

import httpx
import pytest

from app.domain.enums import ManagerType, UnreachableReason
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.infrastructure.bmc_probe import BmcProbe, BmcProbedProvider
from app.infrastructure.providers.factory import PROVIDER_FACTORIES

pytestmark = pytest.mark.unit


def _raising(exc: Exception) -> httpx.MockTransport:
    """
    Transport whose every request raises `exc`.

    Args:
        exc (Exception): The error to raise.

    Returns:
        httpx.MockTransport: The transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Raise.

        Args:
            request (httpx.Request): Ignored.

        Raises:
            Exception: Always `exc`.
        """
        raise exc

    return httpx.MockTransport(handler)


def _tls_error() -> httpx.ConnectError:
    """
    A ConnectError caused by an ssl.SSLError.

    Returns:
        httpx.ConnectError: The error.
    """
    err = httpx.ConnectError("handshake")
    err.__cause__ = ssl.SSLError("bad handshake")
    return err


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (lambda: httpx.Response(200), (True, None)),
        (lambda: httpx.Response(401), (True, None)),
        (lambda: httpx.Response(404), (True, None)),
        (lambda: httpx.Response(302, headers={"location": "/x"}), (True, None)),
        (lambda: _tls_error(), (True, None)),
        (lambda: httpx.ConnectTimeout("t"), (False, UnreachableReason.TIMEOUT)),
        (lambda: httpx.ReadTimeout("t"), (False, UnreachableReason.TIMEOUT)),
        (lambda: httpx.PoolTimeout("t"), (False, UnreachableReason.TIMEOUT)),
        (lambda: httpx.ConnectError("refused"), (False, UnreachableReason.NETWORK_UNREACHABLE)),
        (lambda: OSError("no route"), (False, UnreachableReason.NETWORK_UNREACHABLE)),
    ],
)
async def test_classification(
    make: Callable[[], httpx.Response | Exception],
    expected: tuple[bool, UnreachableReason | None],
) -> None:
    """Any answer is reachable; timeouts and refusals are not."""
    outcome = make()
    if isinstance(outcome, Exception):
        transport = _raising(outcome)
    else:
        transport = httpx.MockTransport(lambda request: outcome)
    async with BmcProbe(transport=transport) as probe:
        assert await probe.probe("10.0.0.1") == expected


async def test_probe_url_and_no_credentials() -> None:
    """One unauthenticated GET to /redfish/v1 on the given host."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Record and answer.

        Args:
            request (httpx.Request): The probe request.

        Returns:
            httpx.Response: 200.
        """
        seen.append(request)
        return httpx.Response(200)

    async with BmcProbe(transport=httpx.MockTransport(handler)) as probe:
        await probe.probe("2001:db8::1")
    assert str(seen[0].url) == "https://[2001:db8::1]/redfish/v1"
    assert "authorization" not in seen[0].headers


class _Inner(ServerInventoryProvider):
    """Fake collector yielding a fixed list."""

    provider_type = "fake"

    def __init__(self, servers: list[ProviderServer]) -> None:
        """
        Store the servers.

        Args:
            servers (list[ProviderServer]): What to yield.
        """
        super().__init__()
        self._servers = servers

    async def health_check(self) -> None:
        """Succeed."""

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Return the first server.

        Args:
            identity (ServerIdentity): Ignored.

        Returns:
            ProviderServer | None: The first server, if any.
        """
        return self._servers[0] if self._servers else None

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield the fixed servers and record an error.

        Yields:
            ProviderServer: Each server.
        """
        self._record_error("slice lost")
        for server in self._servers:
            yield server


def _server(name: str, bmc: str | None) -> ProviderServer:
    """
    A minimal server.

    Args:
        name (str): Server name.
        bmc (str | None): Raw BMC address.

    Returns:
        ProviderServer: The server.
    """
    return ProviderServer(external_id=name, vendor="hp", name=name, bmc_address_raw=bmc)


def _transport_by_host() -> httpx.MockTransport:
    """
    Answer 200 except for host `10.0.0.9`, which times out.

    Returns:
        httpx.MockTransport: The transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Answer or time out by host.

        Args:
            request (httpx.Request): The probe request.

        Returns:
            httpx.Response: 200.

        Raises:
            httpx.ConnectTimeout: For the dead host.
        """
        if request.url.host == "10.0.0.9":
            raise httpx.ConnectTimeout("t")
        return httpx.Response(200)

    return httpx.MockTransport(handler)


async def test_wrapper_stamps_in_order_and_passes_through() -> None:
    """Order is kept, hardware untouched, hostless servers unchanged, errors passed through."""
    servers = [
        _server("a", "redfish-virtualmedia://10.0.0.1:8443/redfish/v1/Systems/1"),
        _server("b", "ipmi://10.0.0.9"),
        _server("c", None),
    ]
    wrapper = BmcProbedProvider(_Inner(servers), transport=_transport_by_host())
    out = [s async for s in wrapper.collect()]
    assert [s.name for s in out] == ["a", "b", "c"]
    assert [s.reachable for s in out] == [True, False, True]
    assert out[1].unreachable_reason is UnreachableReason.TIMEOUT
    assert out[2] is servers[2]
    assert wrapper.collection_errors == ("slice lost",)


async def test_wrapper_get_one_probes() -> None:
    """The single-server path is probed too."""
    wrapper = BmcProbedProvider(
        _Inner([_server("b", "ipmi://10.0.0.9")]), transport=_transport_by_host()
    )
    result = await wrapper.get_one(ServerIdentity())
    assert result is not None
    assert result.reachable is False


def test_only_oneview_factory_wraps() -> None:
    """Non-OneView factories are not wrapped (source check, no construction needed)."""
    import inspect

    assert "BmcProbedProvider" in inspect.getsource(PROVIDER_FACTORIES[ManagerType.ONEVIEW])
    for manager_type, factory in PROVIDER_FACTORIES.items():
        if manager_type is not ManagerType.ONEVIEW:
            assert "BmcProbedProvider" not in inspect.getsource(factory)
