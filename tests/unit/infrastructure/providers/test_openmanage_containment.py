"""OpenManage: one bad profile, address or response never aborts the run."""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs
from tests.unit.infrastructure.providers.test_openmanage_provider import (
    _collected,
    _device,
    _profile,
    _provider,
)

from app.domain.enums import UnreachableReason
from app.domain.ports.provider import ServerIdentity
from app.infrastructure.providers.openmanage.client import OmeClient, OmeConnectionError

pytestmark = pytest.mark.asyncio


async def _run(provider: Any) -> list[Any]:
    """
    Collect a provider to a list.

    Args:
        provider (Any): The provider.

    Returns:
        list[Any]: Everything it yielded.
    """
    return [s async for s in provider.collect()]


async def test_an_unusable_idrac_address_becomes_a_stub_and_the_run_continues() -> None:
    provider, _ = _provider(
        profiles=[_profile("good", "10.0.0.1"), _profile("bad", "evil-device")],
        devices=[
            _device("10.0.0.1", service_tag="GOOD001"),
            _device("evil-device", service_tag="BAD0001", network_address="10.0.0.2 /x"),
        ],
        servers=[_collected("10.0.0.1")],
    )
    servers = await _run(provider)

    [stub] = [s for s in servers if not s.reachable]
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert stub.serial == "BAD0001"
    assert "unusable iDRAC address" in (stub.unreachable_detail or "")
    assert any(s.reachable for s in servers)
    assert provider.collection_errors
    assert provider.server_failures == 1


async def test_a_bare_ipv6_address_is_bracketed_for_the_target() -> None:
    provider, recorded = _provider(
        profiles=[_profile("v6", "x")],
        devices=[_device("x", service_tag="V600001", network_address="FE80::1")],
        servers=[_collected("[fe80::1]")],
    )
    servers = await _run(provider)

    assert recorded["redfish"].targets[0].host == "[fe80::1]"
    assert [s.reachable for s in servers] == [True]
    assert servers[0].bmc_address_raw is not None
    assert "[fe80::1]" in servers[0].bmc_address_raw


async def test_a_merge_crash_costs_only_that_server(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(
        profiles=[_profile("a", "10.0.0.1"), _profile("b", "10.0.0.2")],
        devices=[_device("10.0.0.1", service_tag="A000001"), _device("10.0.0.2", service_tag="B")],
        servers=[
            _collected("10.0.0.1", serial="A000001"),
            _collected("10.0.0.2", serial="B000002"),
        ],
    )
    real = provider._merged

    def flaky(server: Any, by_host: Any) -> Any:
        """
        Merge, except the second server.

        Args:
            server (Any): The Redfish server.
            by_host (Any): Identities.

        Returns:
            Any: The merged server.

        Raises:
            TypeError: For `B000002`.
        """
        if server.serial == "B000002":
            raise TypeError("boom")
        return real(server, by_host)

    monkeypatch.setattr(provider, "_merged", flaky)
    servers = await _run(provider)

    [stub] = [s for s in servers if not s.reachable]
    assert stub.serial == "B000002"
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert len(servers) == 2


async def test_a_redfish_per_system_stub_becomes_an_ome_stub_with_the_cause() -> None:
    inner = dataclasses.replace(
        _collected("10.0.0.9"),
        reachable=False,
        unreachable_reason=UnreachableReason.COLLECTION_ERROR,
        unreachable_detail="mapping: KeyError: 'Status'",
    )
    provider, _ = _provider(
        profiles=[_profile("n", "10.0.0.9")],
        devices=[_device("10.0.0.9", service_tag="N000009")],
        servers=[inner],
    )
    with capture_logs() as logs:
        [stub] = await _run(provider)

    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert stub.unreachable_detail == "mapping: KeyError: 'Status'"
    [event] = [log for log in logs if log["event"] == "ome.server_unreachable"]
    assert event["reason"] == UnreachableReason.COLLECTION_ERROR.value
    assert event["error"] == "mapping: KeyError: 'Status'"


async def test_a_duplicate_address_logs_both_profiles_and_the_first_wins() -> None:
    provider, recorded = _provider(
        profiles=[_profile("first", "10.0.0.1"), _profile("second", "10.0.0.1")],
        devices=[_device("10.0.0.1", service_tag="DUP0001")],
        servers=[_collected("10.0.0.1")],
    )
    with capture_logs() as logs:
        await _run(provider)

    [dup] = [log for log in logs if log["event"] == "ome.duplicate_identity"]
    assert dup["key"] == "10.0.0.1"
    assert dup["kept"]["name"] == "first"
    assert dup["ignored"]["name"] == "second"
    assert len(recorded["redfish"].targets) == 1


async def test_a_duplicate_device_name_logs_and_the_first_wins() -> None:
    provider, _ = _provider(
        profiles=[_profile("p", "10.0.0.1")],
        devices=[_device("10.0.0.1", service_tag="FIRST01"), _device("10.0.0.1", service_tag="X2")],
        servers=[_collected("10.0.0.1", serial=None)],
    )
    with capture_logs() as logs:
        [server] = await _run(provider)

    assert server.serial == "FIRST01"
    assert any(
        log["event"] == "ome.duplicate_identity" and log["kind"] == "device name" for log in logs
    )


async def test_blank_profile_names_get_unique_not_an_idrac_ids() -> None:
    provider, _ = _provider(
        profiles=[
            {**_profile("", "host-a"), "Id": 11},
            {**_profile("", "host-b"), "Id": 12},
        ],
        devices=[],
        servers=[],
    )
    servers = await _run(provider)

    ids = {s.external_id for s in servers}
    assert ids == {"ome-not-an-idrac:profile-11", "ome-not-an-idrac:profile-12"}


async def test_profiles_without_an_address_log_a_name_sample() -> None:
    provider, _ = _provider(
        profiles=[{"ProfileName": f"tpl-{i}", "TemplateName": "t"} for i in range(12)],
        devices=[],
        servers=[],
    )
    with capture_logs() as logs:
        await _run(provider)

    [event] = [log for log in logs if log["event"] == "ome.profile_without_address"]
    assert event["profiles"] == 12
    assert event["sample"] == [f"tpl-{i}" for i in range(10)]


async def test_redfish_failure_counts_reach_the_ome_provider() -> None:
    provider, recorded = _provider(
        profiles=[_profile("n", "10.0.0.1")],
        devices=[_device("10.0.0.1", service_tag="N000001")],
        servers=[_collected("10.0.0.1")],
    )

    async for _server in provider.collect():
        recorded["redfish"].server_failures = 2
    assert provider.server_failures == 2


async def test_get_one_logs_and_explains_a_redfish_recheck_that_returned_nothing() -> None:
    provider, recorded = _provider(
        profiles=[_profile("n", "10.0.0.1")],
        devices=[_device("10.0.0.1", service_tag="N000001")],
        servers=[],
    )

    async def nothing(_identity: ServerIdentity) -> None:
        """
        Return nothing, as a Redfish recheck that failed does.

        Args:
            _identity (ServerIdentity): Ignored.
        """

    original = provider._redfish_provider_factory

    def factory(targets: Any) -> Any:
        """
        Build the fake and give it a failing `get_one`.

        Args:
            targets (Any): The targets.

        Returns:
            Any: The fake.
        """
        fake = original(targets)
        fake.get_one = nothing
        return fake

    provider._redfish_provider_factory = factory
    with capture_logs() as logs:
        stub = await provider.get_one(ServerIdentity(host="x", serial="N000001"))

    assert stub is not None and not stub.reachable
    assert "10.0.0.1" in (stub.unreachable_detail or "")
    assert any(log["event"] == "ome.get_one_failed" for log in logs)
    assert recorded


async def test_a_non_json_body_is_an_ome_connection_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        """
        Answer HTML, whatever is asked.

        Args:
            request (httpx.Request): The request.

        Returns:
            httpx.Response: A 200 with an HTML body.
        """
        return httpx.Response(200, text="<html>proxy login</html>", headers={"X-Auth-Token": "t"})

    client = OmeClient(
        endpoint="ome.example", username="u", password="p", timeout_seconds=1.0, verify_tls=False
    )
    client._http = httpx.AsyncClient(
        base_url="https://ome.example/api", transport=httpx.MockTransport(respond)
    )
    with pytest.raises(OmeConnectionError, match="non-JSON body for /SessionService/Sessions"):
        await client.login()
    with pytest.raises(OmeConnectionError, match="non-JSON body for /ProfileService/Profiles"):
        await client.get_all("/ProfileService/Profiles")
    await client._http.aclose()


async def test_get_one_of_a_redfish_stub_carries_omes_service_tag_not_the_boards_serial() -> None:
    """The inner stub's serial is the board serial; ingest correlates on OME's service tag."""
    inner = dataclasses.replace(
        _collected("10.0.0.1", serial="BOARD-SERIAL"),
        reachable=False,
        unreachable_reason=UnreachableReason.COLLECTION_ERROR,
        unreachable_detail="mapping: KeyError: 'Status'",
    )
    provider, _ = _provider(
        profiles=[_profile("n", "10.0.0.1")],
        devices=[_device("10.0.0.1", service_tag="N000001")],
        servers=[],
    )
    original = provider._redfish_provider_factory

    def factory(targets: Any) -> Any:
        """
        Build the fake and give it a `get_one` that returns the inner stub.

        Args:
            targets (Any): The targets.

        Returns:
            Any: The fake.
        """
        fake = original(targets)

        async def stub(_identity: ServerIdentity) -> Any:
            """
            Return the inner stub.

            Args:
                _identity (ServerIdentity): Ignored.

            Returns:
                Any: The stub.
            """
            return inner

        fake.get_one = stub
        return fake

    provider._redfish_provider_factory = factory
    got = await provider.get_one(ServerIdentity(host="x", serial="N000001"))

    assert got is not None and got.reachable is False
    assert got.serial == "N000001"
    assert got.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert got.unreachable_detail == "mapping: KeyError: 'Status'"
    assert got.external_id == "ome-unreachable:10.0.0.1"
    assert got.name == "n"


async def test_a_duplicate_idrac_address_keeps_the_first_profile_on_purpose() -> None:
    """Intentional: one iDRAC is one server, so the first profile in OME order is the one kept."""
    provider, recorded = _provider(
        profiles=[_profile("first", "10.0.0.1"), _profile("second", "10.0.0.1")],
        devices=[_device("10.0.0.1", service_tag="DUP0001")],
        servers=[_collected("10.0.0.1")],
    )
    with capture_logs() as logs:
        servers = await _run(provider)

    assert len(servers) == 1 and servers[0].reachable
    assert len(recorded["redfish"].targets) == 1
    [dup] = [log for log in logs if log["event"] == "ome.duplicate_identity"]
    assert dup["kind"] == "iDRAC address"
    assert dup["key"] == "10.0.0.1"
    assert (dup["kept"]["name"], dup["ignored"]["name"]) == ("first", "second")
