"""Redfish: one bad system or BMC costs only itself, and every warning names its BMC."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs
from tests.redfish_fixture import RedfishFixture, minimal_service
from tests.unit.infrastructure.providers.test_redfish_collector import (
    _collect,
    _manager,
    _PlainClient,
    _provider,
    _target,
    _with_hgx_baseboard,
)
from tools.run_collector import _is_benign_collection_error

from app.domain.enums import UnreachableReason
from app.domain.ports.provider import SERVER_FAILED_MARKER, ProviderServer, ServerIdentity
from app.domain.services.normalize import normalize_text
from app.infrastructure.providers.redfish import provider as rf
from app.infrastructure.providers.redfish.client import RedfishClient, RedfishProtocolError
from app.infrastructure.providers.redfish.mapping import _as_int, memory_bytes
from app.infrastructure.providers.redfish.provider import (
    AUTH_REJECTED_MARKER,
    UNREACHABLE_MARKER,
    RedfishStandaloneProvider,
    reason_from_error,
)

pytestmark = pytest.mark.unit


def _three_systems() -> dict[str, Any]:
    """
    The minimal service with two more `ComputerSystem`s on the same BMC.

    Returns:
        dict[str, Any]: Resources for a BMC reporting systems 1, 2 and 3.
    """
    resources = minimal_service()
    resources["/redfish/v1/Systems"] = {
        **resources["/redfish/v1/Systems"],
        "Members": [{"@odata.id": f"/redfish/v1/Systems/{i}"} for i in (1, 2, 3)],
    }
    for i in (2, 3):
        resources[f"/redfish/v1/Systems/{i}"] = {
            **resources["/redfish/v1/Systems/1"],
            "@odata.id": f"/redfish/v1/Systems/{i}",
            "Id": str(i),
            "SerialNumber": f"SER{i}",
        }
    return resources


def _assert_partial_not_benign(provider: RedfishStandaloneProvider) -> None:
    """
    Assert the run recorded errors and none carries a benign marker.

    Args:
        provider (RedfishStandaloneProvider): The provider after a run.
    """
    assert provider.collection_errors
    for error in provider.collection_errors:
        assert UNREACHABLE_MARKER not in error
        assert AUTH_REJECTED_MARKER not in error


async def test_a_mapper_crash_on_system_two_keeps_its_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Systems 1 and 3 are mapped; 2 is a COLLECTION_ERROR stub with a readable cause."""
    real = rf.system_to_provider_server

    def flaky(system: dict[str, Any], **kwargs: Any) -> ProviderServer:
        """
        Map, except system 2.

        Args:
            system (dict[str, Any]): The raw System.
            **kwargs (Any): Passed on.

        Returns:
            ProviderServer: The mapped server.

        Raises:
            KeyError: For system 2.
        """
        if system["Id"] == "2":
            raise KeyError("Status")
        return real(system, **kwargs)

    monkeypatch.setattr(rf, "system_to_provider_server", flaky)
    with RedfishFixture(resources=_three_systems()) as fixture:
        provider = _provider(fixture.port)
        servers = await _collect(provider)

    assert len(servers) == 3
    [stub] = [s for s in servers if not s.reachable]
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert "KeyError" in (stub.unreachable_detail or "")
    assert stub.external_id == "redfish://127.0.0.1/redfish/v1/Systems/2"
    assert {s.serial for s in servers if s.reachable} == {"FCH2201V0AB", "SER3"}
    assert provider.server_failures == 1
    _assert_partial_not_benign(provider)


async def test_a_failing_tray_marks_its_host_instead_of_dropping_gpus_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tray whose GPUs cannot be read must not let the host be ingested short of GPUs."""

    def broken(*_: Any, **__: Any) -> Any:
        """
        Fail.

        Raises:
            TypeError: Always.
        """
        raise TypeError("bad tray")

    monkeypatch.setattr(rf, "gpus_from_processors", broken)
    with RedfishFixture(resources=_with_hgx_baseboard(minimal_service())) as fixture:
        provider = _provider(fixture.port)
        servers = await _collect(provider)

    [stub] = servers
    assert stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert "GPU baseboard" in (stub.unreachable_detail or "")


async def test_an_unexpected_error_on_one_host_leaves_the_other_hosts_alone() -> None:
    """A TypeError out of the client factory is that host's stub, not a run abort."""
    with RedfishFixture(resources=minimal_service()) as fixture:
        good = _target(fixture.port)
        bad = _target(fixture.port, host="boom")

        def factory(target: Any) -> Any:
            """
            Build a client, except for the host that blows up.

            Args:
                target (Any): The BMC.

            Returns:
                Any: A client.

            Raises:
                TypeError: For `boom`.
            """
            if target.host == "boom":
                raise TypeError("'NoneType' object is not subscriptable")
            return _PlainClient(target=target, connect_timeout=2.0, read_timeout=5.0)

        provider = RedfishStandaloneProvider(
            manager=_manager(),
            targets=[good, bad],
            connect_timeout=2.0,
            read_timeout=5.0,
            host_budget_seconds=20.0,
            run_budget_seconds=60.0,
            fleet_concurrency=2,
            client_factory=factory,
        )
        with capture_logs() as logs:
            servers = await _collect(provider)

    [stub] = [s for s in servers if not s.reachable]
    assert stub.external_id == "redfish-unreachable:boom"
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert "TypeError" in (stub.unreachable_detail or "")
    assert any(s.reachable and s.serial == "FCH2201V0AB" for s in servers)
    _assert_partial_not_benign(provider)
    assert reason_from_error(provider.collection_errors[0]) is UnreachableReason.COLLECTION_ERROR
    assert any(log["event"] == "redfish.host_failed_unexpected" for log in logs)


async def test_json_infinity_does_not_fail_the_server() -> None:
    """`Infinity` parses to a float and `int(inf)` raises OverflowError."""
    assert _as_int(float("inf")) is None
    assert _as_int("inf") is None
    assert memory_bytes({"MemorySummary": {"TotalSystemMemoryGiB": float("inf")}}, None) is None
    resources = minimal_service()
    resources["/redfish/v1/Systems/1"] = {
        **resources["/redfish/v1/Systems/1"],
        "MemorySummary": {"TotalSystemMemoryGiB": float("inf")},
        "ProcessorSummary": {"Count": float("inf"), "CoreCount": float("inf")},
    }
    with RedfishFixture(resources=resources) as fixture:
        servers = await _collect(_provider(fixture.port))

    [server] = servers
    assert server.reachable and server.serial == "FCH2201V0AB"
    assert json.dumps(float("inf")) == "Infinity"


async def test_malformed_links_are_unread_not_fatal() -> None:
    """`Links.Chassis`/`ManagedBy` as a dict, `Oem` as a list: the server is still mapped."""
    resources = minimal_service()
    resources["/redfish/v1/Systems/1"] = {
        **resources["/redfish/v1/Systems/1"],
        "Links": {"Chassis": {"@odata.id": "/x"}, "ManagedBy": "nope"},
        "Oem": ["not", "a", "dict"],
    }
    with RedfishFixture(resources=resources) as fixture:
        servers = await _collect(_provider(fixture.port))

    [server] = servers
    assert server.reachable and server.serial == "FCH2201V0AB"
    assert server.bmc_mac is None


async def test_a_400_on_a_sub_resource_keeps_the_server_and_names_host_system_and_path() -> None:
    path = "/redfish/v1/Systems/HGX_Baseboard_0/Processors"
    resources = _with_hgx_baseboard(minimal_service())
    with RedfishFixture(resources=resources, faults={path: 400}) as fixture, capture_logs() as logs:
        servers = await _collect(_provider(fixture.port))

    assert servers
    [skipped] = [
        log
        for log in logs
        if log["event"] == "redfish.resource_skipped"
        and log.get("resource") == "Processors"
        and log.get("system") == "/redfish/v1/Systems/HGX_Baseboard_0"
    ]
    assert skipped["host"] == "127.0.0.1"
    assert skipped["path"] == path
    assert "400" in skipped["error"]


async def test_a_400_on_the_systems_collection_yields_a_stub_naming_the_path() -> None:
    with RedfishFixture(resources=minimal_service(), faults={"/redfish/v1/Systems": 400}) as f:
        provider = _provider(f.port)
        [stub] = await _collect(provider)

    assert stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.PROTOCOL_ERROR
    assert "/redfish/v1/Systems" in (stub.unreachable_detail or "")
    assert "400" in (stub.unreachable_detail or "")


async def test_every_host_level_stub_carries_a_detail() -> None:
    [stub] = await _collect(_provider(1, _target(1), connect_timeout=0.2))
    assert stub.unreachable_reason is UnreachableReason.NETWORK_UNREACHABLE
    assert (stub.unreachable_detail or "").startswith("unreachable:")
    with RedfishFixture(resources=minimal_service()) as fixture:
        [rejected] = await _collect(_provider(fixture.port, _target(fixture.port, password="x")))
    assert rejected.unreachable_detail == "login rejected for credential 'test'"


async def test_an_invalid_host_url_is_a_protocol_error_not_a_crash() -> None:
    with pytest.raises(RedfishProtocolError, match="not a usable BMC address"):
        RedfishClient(
            target=_target(443, host="bmc\n1"),
            connect_timeout=1.0,
            read_timeout=1.0,
            tls_min_version="TLSv1_2",
            debug_http=False,
        )


async def test_httpx_write_and_decoding_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.infrastructure.providers.redfish.client import RedfishUnreachableError

    client = _PlainClient(target=_target(1), connect_timeout=0.2, read_timeout=0.2)

    async def write_error(*_: Any, **__: Any) -> Any:
        """
        Fail like a dropped socket.

        Raises:
            httpx.WriteError: Always.
        """
        raise httpx.WriteError("broken pipe")

    monkeypatch.setattr(client._client, "request", write_error)
    with pytest.raises(RedfishUnreachableError):
        await client._send("GET", "/redfish/v1/", authenticated=False)

    async def decoding_error(*_: Any, **__: Any) -> Any:
        """
        Fail like a bad gzip body.

        Raises:
            httpx.DecodingError: Always.
        """
        raise httpx.DecodingError("bad gzip")

    monkeypatch.setattr(client._client, "request", decoding_error)
    with pytest.raises(RedfishProtocolError, match="DecodingError"):
        await client._send("GET", "/redfish/v1/", authenticated=False)
    await client._client.aclose()


async def test_a_service_root_with_non_dict_session_links_is_a_protocol_error() -> None:
    resources = minimal_service()
    resources["/redfish/v1/"] = {
        **resources["/redfish/v1/"],
        "Links": {"Sessions": "nope"},
        "SessionService": "nope",
    }
    with RedfishFixture(resources=resources) as fixture:
        provider = _provider(fixture.port)
        [stub] = await _collect(provider)

    assert stub.unreachable_reason is UnreachableReason.PROTOCOL_ERROR
    assert "Sessions" in (stub.unreachable_detail or "")


async def test_get_one_failures_are_logged_not_silent() -> None:
    with RedfishFixture(resources=minimal_service()) as fixture:
        provider = _provider(fixture.port, _target(fixture.port, password="wrong"))
        with capture_logs() as logs:
            assert await provider.get_one(ServerIdentity(host="127.0.0.1", serial=None)) is None
            assert await provider.get_one(ServerIdentity(host="10.9.9.9", serial=None)) is None

    failed = [log for log in logs if log["event"] == "redfish.get_one_failed"]
    assert [log["host"] for log in failed] == ["127.0.0.1", "10.9.9.9"]
    assert failed[0]["error_type"] == "RedfishAuthError"
    assert "not in this run's configured targets" in failed[1]["error"]


async def test_the_run_budget_log_lists_the_unfinished_hosts() -> None:
    class _Hang:
        """A client whose login never finishes."""

        async def __aenter__(self) -> _Hang:
            """
            Hang.

            Returns:
                _Hang: Never.
            """
            await asyncio.sleep(30)
            return self

        async def __aexit__(self, *_: object) -> None:
            """Nothing to close."""

    targets = [_target(1, host=f"10.0.0.{i}") for i in range(1, 13)]
    provider = RedfishStandaloneProvider(
        manager=_manager(),
        targets=targets,
        connect_timeout=1.0,
        read_timeout=1.0,
        host_budget_seconds=30.0,
        run_budget_seconds=0.3,
        fleet_concurrency=12,
        client_factory=lambda _t: _Hang(),
    )
    with capture_logs() as logs:
        await _collect(provider)

    [budget] = [log for log in logs if log["event"] == "redfish.run_budget_exceeded"]
    assert budget["hosts_unfinished"] == 12
    assert budget["unfinished_hosts"].endswith("and 2 more")
    assert any("and 2 more" in e for e in provider.collection_errors)


def _raise_for_system(monkeypatch: pytest.MonkeyPatch, system_id: str) -> None:
    """
    Make the real mapper raise for one System id and map the rest.

    Args:
        monkeypatch (pytest.MonkeyPatch): The fixture.
        system_id (str): The `Id` of the System that fails.
    """
    real = rf.system_to_provider_server

    def flaky(system: dict[str, Any], **kwargs: Any) -> ProviderServer:
        """
        Map, except the chosen system.

        Args:
            system (dict[str, Any]): The raw System.
            **kwargs (Any): Passed on.

        Returns:
            ProviderServer: The mapped server.

        Raises:
            KeyError: For the chosen system.
        """
        if system["Id"] == system_id:
            raise KeyError("Status")
        return real(system, **kwargs)

    monkeypatch.setattr(rf, "system_to_provider_server", flaky)


def _identity(ps: ProviderServer) -> tuple[Any, ...]:
    """
    The fields ingest correlates on, plus name and manager.

    Args:
        ps (ProviderServer): A provider server.

    Returns:
        tuple[Any, ...]: Vendor, normalized serial, external id, name, manager id.
    """
    return (ps.vendor, normalize_text(ps.serial), ps.external_id, ps.name, ps.manager_id)


async def _stub_and_clean(
    monkeypatch: pytest.MonkeyPatch, resources: dict[str, Any], **target_overrides: Any
) -> tuple[ProviderServer, ProviderServer]:
    """
    Collect system 1 cleanly, then again with its mapping failing.

    Args:
        monkeypatch (pytest.MonkeyPatch): The fixture.
        resources (dict[str, Any]): The BMC's resources.
        **target_overrides (Any): Fields replaced on the target (e.g. `name`).

    Returns:
        tuple[ProviderServer, ProviderServer]: The stub and the clean mapping.
    """
    with RedfishFixture(resources=resources) as fixture:
        target = dataclasses.replace(_target(fixture.port), **target_overrides)
        [clean] = await _collect(_provider(fixture.port, target))
        _raise_for_system(monkeypatch, "1")
        [stub] = await _collect(_provider(fixture.port, target))
    assert clean.reachable and stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    return stub, clean


async def test_the_stub_has_the_same_identity_as_a_clean_mapping_of_the_same_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different identity would make ingest write a second document."""
    stub, clean = await _stub_and_clean(monkeypatch, minimal_service())
    assert _identity(stub) == _identity(clean)


async def test_a_dell_stub_carries_the_service_tag_not_the_board_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NodeID` differs from `SerialNumber`: the stub must use the one the mapper uses."""
    resources = minimal_service()
    resources["/redfish/v1/Systems/1"] = {
        **resources["/redfish/v1/Systems/1"],
        "Oem": {"Dell": {"DellSystem": {"NodeID": "SVCTAG1"}}},
    }
    stub, clean = await _stub_and_clean(monkeypatch, resources)
    assert clean.serial == "SVCTAG1"
    assert stub.serial == "SVCTAG1"
    assert _identity(stub) == _identity(clean)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"HostName": "the-host", "Name": "Other"}, "the-host"),
        ({"HostName": None, "Name": "  Padded Name  "}, "Padded Name"),
    ],
)
async def test_a_stub_is_named_like_the_mapper_names_the_server(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], expected: str
) -> None:
    """HostName wins over Name, and surrounding whitespace is stripped."""
    resources = minimal_service()
    resources["/redfish/v1/Systems/1"] = {**resources["/redfish/v1/Systems/1"], **body}
    stub, clean = await _stub_and_clean(monkeypatch, resources)
    assert clean.name == expected
    assert stub.name == expected


async def test_the_operator_name_override_wins_for_the_stub_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`target.name` beats HostName in the mapping and in the stub."""
    stub, clean = await _stub_and_clean(monkeypatch, minimal_service(), name="operator-name")
    assert clean.name == "operator-name"
    assert stub.name == "operator-name"


def _without_serial(resources: dict[str, Any], system_id: str) -> None:
    """
    Blank one System's serial in place.

    Args:
        resources (dict[str, Any]): The BMC's resources.
        system_id (str): The System to blank.
    """
    path = f"/redfish/v1/Systems/{system_id}"
    resources[path] = {**resources[path], "SerialNumber": ""}


async def test_a_serial_less_failure_on_a_multi_system_bmc_gets_no_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stub without a serial would be matched by BMC host to the wrong sibling."""
    resources = _three_systems()
    _without_serial(resources, "2")
    _raise_for_system(monkeypatch, "2")
    with RedfishFixture(resources=resources) as fixture:
        provider = _provider(fixture.port)
        servers = await _collect(provider)

    assert sorted(s.external_id for s in servers) == [
        "redfish://127.0.0.1/redfish/v1/Systems/1",
        "redfish://127.0.0.1/redfish/v1/Systems/3",
    ]
    assert all(s.reachable for s in servers)
    assert provider.server_failures == 1
    _assert_partial_not_benign(provider)


async def test_a_serial_less_failure_on_a_single_system_bmc_still_gets_a_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lone System is matched by BMC host (ADR-0037), so the stub is safe."""
    resources = minimal_service()
    _without_serial(resources, "1")
    _raise_for_system(monkeypatch, "1")
    with RedfishFixture(resources=resources) as fixture:
        [stub] = await _collect(_provider(fixture.port))

    assert stub.reachable is False
    assert stub.serial is None
    assert stub.external_id == "redfish://127.0.0.1/redfish/v1/Systems/1"


def _fail_processors_read_of(monkeypatch: pytest.MonkeyPatch, system_id: str) -> None:
    """
    Make the early sub-resource read raise for one System.

    Args:
        monkeypatch (pytest.MonkeyPatch): The fixture.
        system_id (str): The `Id` whose `Processors` read fails.
    """
    real = RedfishStandaloneProvider._optional

    async def flaky(self: Any, client: Any, system: dict[str, Any], key: str) -> Any:
        """
        Read, except `Processors` of the chosen system.

        Args:
            self (Any): The provider.
            client (Any): The client.
            system (dict[str, Any]): The raw System.
            key (str): The link property.

        Returns:
            Any: The members.

        Raises:
            TypeError: For the chosen system's `Processors`.
        """
        if key == "Processors" and system["Id"] == system_id:
            raise TypeError("bad processors")
        return await real(self, client, system, key)

    monkeypatch.setattr(RedfishStandaloneProvider, "_optional", flaky)


async def test_an_early_failure_on_a_multi_system_bmc_gets_no_stub_and_marks_gpus_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed System may be a GPU tray: no ghost document, and siblings' GPUs are unread."""
    _fail_processors_read_of(monkeypatch, "2")
    with RedfishFixture(resources=_three_systems()) as fixture:
        provider = _provider(fixture.port)
        servers = await _collect(provider)

    assert sorted(s.external_id for s in servers) == [
        "redfish://127.0.0.1/redfish/v1/Systems/1",
        "redfish://127.0.0.1/redfish/v1/Systems/3",
    ]
    assert all(s.reachable and s.gpus is None for s in servers)
    assert provider.server_failures == 1


async def test_an_early_failure_on_a_single_system_bmc_gets_a_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one System there is no sibling to confuse, so the failure is visible."""
    _fail_processors_read_of(monkeypatch, "1")
    with RedfishFixture(resources=minimal_service()) as fixture:
        [stub] = await _collect(_provider(fixture.port))

    assert stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert stub.serial == "FCH2201V0AB"


@pytest.mark.parametrize(
    "text",
    ["boom", "x: unreachable \u2014 y", "x: login failed for credential 'c'"],
)
async def test_an_unexpected_host_error_is_never_benign_whatever_its_text_says(text: str) -> None:
    """The text of a bug must not smuggle a benign marker past the exit-code logic."""
    with RedfishFixture(resources=minimal_service()) as fixture:

        def factory(target: Any) -> Any:
            """
            Fail with the parametrized text.

            Args:
                target (Any): The BMC.

            Raises:
                TypeError: Always.
            """
            raise TypeError(text)

        provider = RedfishStandaloneProvider(
            manager=_manager(),
            targets=[_target(fixture.port)],
            connect_timeout=2.0,
            read_timeout=5.0,
            host_budget_seconds=20.0,
            run_budget_seconds=60.0,
            fleet_concurrency=1,
            client_factory=factory,
        )
        [stub] = await _collect(provider)

    assert provider.server_failures == 1
    [message] = provider.collection_errors
    assert SERVER_FAILED_MARKER in message
    assert reason_from_error(message) is UnreachableReason.COLLECTION_ERROR
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert not _is_benign_collection_error(message)


async def test_get_one_with_a_hung_client_returns_none_and_logs_the_timeout() -> None:
    class _Hang:
        """A client whose login never finishes."""

        async def __aenter__(self) -> _Hang:
            """
            Hang.

            Returns:
                _Hang: Never.
            """
            await asyncio.sleep(30)
            return self

        async def __aexit__(self, *_: object) -> None:
            """Nothing to close."""

    provider = RedfishStandaloneProvider(
        manager=_manager(),
        targets=[_target(1)],
        connect_timeout=1.0,
        read_timeout=1.0,
        host_budget_seconds=0.05,
        run_budget_seconds=60.0,
        fleet_concurrency=1,
        client_factory=lambda _t: _Hang(),
    )
    with capture_logs() as logs:
        assert await provider.get_one(ServerIdentity(host="127.0.0.1", serial=None)) is None

    [event] = [log for log in logs if log["event"] == "redfish.get_one_failed"]
    assert event["host"] == "127.0.0.1"
    assert event["error_type"] == "TimeoutError"
    assert event["error"] == "timed out"


async def test_a_rejected_login_on_one_host_is_that_hosts_stub_and_the_rest_are_collected() -> None:
    with RedfishFixture(resources=minimal_service()) as fixture:
        good = _target(fixture.port, host="good.test")
        bad = _target(fixture.port, host="bad.test", password="wrong")
        provider = _provider(fixture.port, good, bad, connect_host="127.0.0.1")
        servers = await _collect(provider)

    [stub] = [s for s in servers if not s.reachable]
    assert stub.external_id == "redfish-unreachable:bad.test"
    assert stub.unreachable_reason is UnreachableReason.AUTH_REJECTED
    assert stub.unreachable_detail == "login rejected for credential 'test'"
    [ok] = [s for s in servers if s.reachable]
    assert ok.external_id == "redfish://good.test/redfish/v1/Systems/1"
    assert ok.serial == "FCH2201V0AB"
    assert provider.server_failures == 0
