"""BMC probe classification and the OneView wrapper (ADR-0037, decision 3)."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator, Callable

import httpx
import pytest
from structlog.testing import capture_logs

from app.domain.enums import ManagerType, UnreachableReason
from app.domain.ports.provider import (
    SERVER_FAILED_MARKER,
    ProviderServer,
    ServerIdentity,
    ServerInventoryProvider,
)
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


class _Exploding(ServerInventoryProvider):
    """Yields servers, then raises, to test the mid-batch flush."""

    provider_type = "ONEVIEW"

    def __init__(self, servers: list[ProviderServer]) -> None:
        """
        Args:
            servers (list[ProviderServer]): Yielded before the failure.
        """
        super().__init__()
        self._servers = servers

    async def health_check(self) -> None:
        """Always healthy."""

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Not used.

        Args:
            identity (ServerIdentity): Ignored.

        Returns:
            ProviderServer | None: Always `None`.
        """
        return None

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield the servers, then fail.

        Yields:
            ProviderServer: Each server.

        Raises:
            RuntimeError: After the last server.
        """
        for server in self._servers:
            yield server
        raise RuntimeError("inner broke")


async def test_inner_failure_still_flushes_the_buffered_batch() -> None:
    """Already-mapped servers are probed and yielded before the error propagates."""
    wrapper = BmcProbedProvider(
        _Exploding([_server("a", "ipmi://10.0.0.1"), _server("b", "ipmi://10.0.0.9")]),
        transport=_transport_by_host(),
    )
    out: list[ProviderServer] = []
    with pytest.raises(RuntimeError, match="inner broke"):
        async for server in wrapper.collect():
            out.append(server)  # noqa: PERF401 - the iteration raises midway
    assert [s.name for s in out] == ["a", "b"]
    assert [s.reachable for s in out] == [True, False]


async def test_a_server_whose_probe_step_raises_keeps_its_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One probe-step crash marks that server COLLECTION_ERROR; the others are stamped."""
    real = BmcProbe.probe

    async def flaky(self: BmcProbe, host: str, port: int = 443) -> tuple[bool, None]:
        """
        Probe, except host 10.0.0.2.

        Args:
            self (BmcProbe): The probe.
            host (str): The host.
            port (int): The port.

        Returns:
            tuple[bool, None]: The real result.

        Raises:
            RuntimeError: For 10.0.0.2.
        """
        if host == "10.0.0.2":
            raise RuntimeError("probe blew up")
        return await real(self, host, port)  # ty: ignore[invalid-return-type]

    monkeypatch.setattr(BmcProbe, "probe", flaky)
    servers = [_server(n, f"ipmi://10.0.0.{i}") for i, n in enumerate("abc", 1)]
    wrapper = BmcProbedProvider(_Inner(servers), transport=_transport_by_host())
    with capture_logs() as logs:
        out = [s async for s in wrapper.collect()]
    assert [s.name for s in out] == ["a", "b", "c"]
    assert [s.reachable for s in out] == [True, False, True]
    assert out[1].unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert out[1].unreachable_detail is not None
    assert "RuntimeError: probe blew up" in out[1].unreachable_detail
    event = next(e for e in logs if e["event"] == "bmc_probe.server_failed")
    assert event["server"] == "b"
    assert event["error"] == "probe blew up"


async def test_invalid_host_is_a_collection_error_not_a_network_fault() -> None:
    """A malformed host (httpx.InvalidURL) is classified COLLECTION_ERROR with a detail."""
    async with BmcProbe(transport=_raising(httpx.InvalidURL("bad host"))) as probe:
        assert await probe.probe("10.0.0.1") == (False, UnreachableReason.COLLECTION_ERROR)
    wrapper = BmcProbedProvider(
        _Inner([_server("a", "ipmi://10.0.0.1")]),
        transport=_raising(httpx.InvalidURL("bad host")),
    )
    result = await wrapper.get_one(ServerIdentity())
    assert result is not None
    assert result.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert result.unreachable_detail is not None


async def test_a_bad_bracket_address_does_not_raise() -> None:
    """An unparseable address leaves the server unprobed instead of raising."""
    wrapper = BmcProbedProvider(
        _Inner([_server("a", "https://[::1")]), transport=_transport_by_host()
    )
    out = [s async for s in wrapper.collect()]
    assert [s.name for s in out] == ["a"]


async def test_an_unexpected_probe_exception_is_a_collection_error_logged_at_error() -> None:
    """Not NETWORK_UNREACHABLE: a bug in the probe must not look like a dead BMC."""
    async with BmcProbe(transport=_raising(RuntimeError("weird"))) as probe:
        with capture_logs() as logs:
            result = await probe.probe("10.0.0.1")
    assert result == (False, UnreachableReason.COLLECTION_ERROR)
    event = next(e for e in logs if e["event"] == "bmc_probe.unexpected_error")
    assert event["log_level"] == "error"
    assert event["error"] == "weird"


class _Counted(_Inner):
    """An inner collector that already failed 3 of 7 servers itself."""

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield the servers after faking the inner counters.

        Yields:
            ProviderServer: Each server.
        """
        self._server_failures = 3
        self._servers_seen = 7
        async for server in super()._list_servers():
            yield server


async def test_the_wrapper_adds_its_own_probe_failures_to_the_inner_counts() -> None:
    """`server_failures` and `collection_errors` are inner plus own; each own one is marked."""
    inner = _Counted([_server("a", "ipmi://10.0.0.1"), _server("b", "ipmi://10.0.0.2")])
    wrapper = BmcProbedProvider(inner, transport=_raising(httpx.InvalidURL("bad host")))

    out = [s async for s in wrapper.collect()]

    assert [s.unreachable_reason for s in out] == [UnreachableReason.COLLECTION_ERROR] * 2
    assert wrapper.servers_seen == inner.servers_seen
    assert inner.server_failures == 3
    assert wrapper.server_failures == 5
    own = wrapper.collection_errors[len(inner.collection_errors) :]
    assert len(own) == 2
    assert all(SERVER_FAILED_MARKER in m and "probing its BMC" in m for m in own)
    assert wrapper.collection_errors[0] == "slice lost"
    assert len(wrapper.collection_errors) == len(inner.collection_errors) + 2


async def test_the_inner_counters_pass_through_with_no_probe_failure() -> None:
    inner = _Counted([_server("a", "ipmi://10.0.0.1")])
    wrapper = BmcProbedProvider(inner, transport=_transport_by_host())

    [s async for s in wrapper.collect()]

    assert (wrapper.servers_seen, wrapper.server_failures) == (inner.servers_seen, 3)
    assert wrapper.collection_errors == ("slice lost",)
