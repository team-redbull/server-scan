"""OneView: one bad server never aborts the run; `get_one` tells "gone" from "unreachable"."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs
from tests.unit.infrastructure.providers.test_oneview_provider import (
    _appliance,
    _collect,
    _hardware,
    _provider,
)

from app.domain.enums import UnreachableReason
from app.domain.ports.provider import ServerIdentity
from app.domain.services.normalize import normalize_text
from app.infrastructure.providers.oneview import provider as ov_provider
from app.infrastructure.providers.oneview.client import OneViewConnectionError

pytestmark = pytest.mark.unit


def _fleet() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Three profiled servers a, b, c.

    Returns:
        tuple[list[dict[str, Any]], list[dict[str, Any]]]: Profiles and hardware.
    """
    profiles = [{"uri": f"/rest/server-profiles/{k}", "name": f"srv-{k}"} for k in "abc"]
    hardware = [
        _hardware(f"/rest/server-hardware/{k}", profile_uri=f"/rest/server-profiles/{k}")
        for k in "abc"
    ]
    return profiles, hardware


def _handler(profiles: list[dict[str, Any]], hardware: list[dict[str, Any]]) -> Any:
    """
    A transport handler for the fleet, via the shared helper.

    Args:
        profiles (list[dict[str, Any]]): Profiles.
        hardware (list[dict[str, Any]]): Hardware.

    Returns:
        Any: The handler.
    """
    return _appliance(profiles=profiles, hardware=hardware)


async def test_a_mapper_crash_on_the_second_server_costs_only_that_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Servers 1 and 3 are yielded; 2 is a COLLECTION_ERROR stub keyed by its serial."""
    real = ov_provider.server_from

    def flaky(**kwargs: Any) -> Any:
        """
        Map, except server b.

        Args:
            **kwargs: Passed to the real mapper.

        Returns:
            Any: The mapped server.

        Raises:
            ValueError: For server b.
        """
        if kwargs["hardware"]["uri"].endswith("/b"):
            raise ValueError("boom")
        return real(**kwargs)

    monkeypatch.setattr(ov_provider, "server_from", flaky)
    profiles, hardware = _fleet()
    provider = _provider(_handler(profiles, hardware))
    with capture_logs() as logs:
        servers = await _collect(provider)

    assert [s.name for s in servers] == ["srv-a", "srv-b", "srv-c"]
    stub = servers[1]
    assert stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert stub.serial == "B"
    assert stub.vendor == "hp"
    assert stub.external_id == "/rest/server-hardware/b"
    assert stub.unreachable_detail is not None and "ValueError" in stub.unreachable_detail
    assert provider.server_failures == 1
    assert any("srv-b" in e and "ValueError" in e for e in provider.collection_errors)
    event = next(entry for entry in logs if entry["event"] == "oneview.server_failed")
    assert event["hardware_uri"] == "/rest/server-hardware/b"
    assert event["profile_name"] == "srv-b"
    assert event["serial"] == "B"


def _get_one_provider(status: int | None) -> Any:
    """
    A provider whose appliance answers every hardware GET with `status` (or refuses the socket).

    Args:
        status (int | None): The HTTP status, or `None` for a connection failure.

    Returns:
        Any: The provider.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        """
        Answer the login, fail the rest.

        Args:
            request (httpx.Request): The request.

        Returns:
            httpx.Response: The answer.

        Raises:
            httpx.ConnectError: When `status` is `None`.
        """
        if request.url.path == "/rest/version":
            return httpx.Response(200, json={"currentVersion": 8000, "minimumVersion": 1})
        if request.url.path == "/rest/login-sessions":
            return httpx.Response(
                204 if request.method == "DELETE" else 200, json={"sessionID": "t"}
            )
        if status is None:
            raise httpx.ConnectError("no route")
        return httpx.Response(status, json={})

    return _provider(handle)


async def test_get_one_404_means_gone() -> None:
    """A 404 on the hardware URI is `None`."""
    provider = _get_one_provider(404)
    assert await provider.get_one(ServerIdentity(external_id="/rest/server-hardware/a")) is None


@pytest.mark.parametrize("status", [None, 500, 503])
async def test_get_one_transient_failure_raises_and_is_logged(status: int | None) -> None:
    """A connection failure or 5xx must not read as "server gone"."""
    provider = _get_one_provider(status)
    with capture_logs() as logs, pytest.raises(OneViewConnectionError):
        await provider.get_one(ServerIdentity(external_id="/rest/server-hardware/a"))
    event = next(e for e in logs if e["event"] == "oneview.get_one_failed")
    assert event["uri"] == "/rest/server-hardware/a"


async def test_unreadable_power_supplies_warning_names_causes_and_uris() -> None:
    """The warning carries a Counter of causes and a capped URI sample."""
    profiles, hardware = _fleet()
    provider = _provider(_handler(profiles, hardware), collect_psus=True)
    with capture_logs() as logs:
        await _collect(provider)
    event = next(e for e in logs if e["event"] == "oneview.power_supplies_unreadable")
    assert event["servers"] == 3
    assert sum(event["causes"].values()) == 3
    assert event["sample_uris"] == sorted(h["uri"] for h in hardware)


async def test_profile_without_name_and_duplicate_uri_are_logged() -> None:
    """Dropped and overwritten profiles are no longer silent."""
    profiles, hardware = _fleet()
    profiles.append({"uri": "/rest/server-profiles/z"})
    profiles.append(dict(profiles[0]))
    provider = _provider(_handler(profiles, hardware))
    with capture_logs() as logs:
        await _collect(provider)
    events = {e["event"]: e for e in logs}
    assert events["oneview.profiles_dropped"]["profiles"] == 1
    assert events["oneview.profile_uri_duplicated"]["sample_uris"] == ["/rest/server-profiles/a"]


async def test_unassigned_hardware_log_carries_a_sample() -> None:
    """`hardware_without_profile` names which hardware."""
    profiles, hardware = _fleet()
    hardware.append(_hardware("/rest/server-hardware/x", profile_uri=None))
    provider = _provider(_handler(profiles, hardware))
    with capture_logs() as logs:
        await _collect(provider)
    event = next(e for e in logs if e["event"] == "oneview.hardware_without_profile")
    assert event["sample"] == ["/rest/server-hardware/x"]


async def test_the_stub_has_the_same_identity_as_a_clean_mapping_of_the_same_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different identity would make ingest write a second document for server b."""
    profiles, hardware = _fleet()
    clean = (await _collect(_provider(_handler(profiles, hardware))))[1]
    real = ov_provider.server_from

    def flaky(**kwargs: Any) -> Any:
        """
        Map, except server b.

        Args:
            **kwargs: Passed to the real mapper.

        Returns:
            Any: The mapped server.

        Raises:
            ValueError: For server b.
        """
        if kwargs["hardware"]["uri"].endswith("/b"):
            raise ValueError("boom")
        return real(**kwargs)

    monkeypatch.setattr(ov_provider, "server_from", flaky)
    stub = (await _collect(_provider(_handler(profiles, hardware))))[1]
    assert stub.reachable is False and clean.reachable is True
    assert _identity(stub) == _identity(clean)


def _identity(ps: Any) -> tuple[Any, ...]:
    """
    The fields ingest correlates on, plus name and manager.

    Args:
        ps (Any): A provider server.

    Returns:
        tuple[Any, ...]: Vendor, normalized serial, external id, name, manager id.
    """
    return (ps.vendor, normalize_text(ps.serial), ps.external_id, ps.name, ps.manager_id)
