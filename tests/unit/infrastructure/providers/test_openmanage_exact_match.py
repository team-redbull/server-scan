"""The live OME lookup (`get_one`) must pick the exact record, never the first one OME returns.

A real OME treats `$filter=<name field> eq 'x'` as a PREFIX match and returns the rows unsorted
(confirmed live 2026-11-25: `TargetName eq '10.0.0.1'` also returned 10.0.0.10 to 10.0.0.18), so
taking `rows[0]` attached another server's profile name to the server being refreshed. These tests
use a fake that behaves like that OME, with the wrong row first.

See docs/adr/0069-ome-live-lookup-matches-exactly.md.
"""

from __future__ import annotations

import re
from typing import Any, Self

import pytest

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ServerIdentity
from app.infrastructure.providers.openmanage.mapping import OmeIdentity
from app.infrastructure.providers.openmanage.provider import OpenManageProvider
from app.infrastructure.providers.redfish.targets import RedfishCredential

pytestmark = pytest.mark.asyncio

_FILTER = re.compile(r"\$filter=(\w+) eq '((?:[^']|'')*)'")


class _PrefixOme:
    """An OME whose `$filter ... eq` is a prefix match, with the rows in the wrong order."""

    def __init__(self, profiles: list[dict[str, Any]], devices: list[dict[str, Any]]) -> None:
        self._profiles = profiles
        self._devices = devices
        self.paths: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return

    async def get_all(self, path: str) -> list[dict[str, Any]]:
        self.paths.append(path)
        rows = self._profiles if "/Profiles" in path else self._devices
        match = _FILTER.search(path)
        if match is None:
            return list(reversed(rows))
        field, value = match.group(1), match.group(2).replace("''", "'")
        fold = field == "DeviceServiceTag"
        want = value.casefold() if fold else value
        return [
            r
            for r in reversed(rows)
            if (str(r.get(field) or "").casefold() if fold else str(r.get(field) or "")).startswith(
                want
            )
        ]


def _provider() -> OpenManageProvider:
    return OpenManageProvider(
        manager=Manager(
            _id="mgr-ome-1",
            name="ome-1",
            type=ManagerType.OPENMANAGE,
            endpoint="ome.example",
            audit=AuditFields.new(),
        ),
        credentials=ManagerConnection(endpoint="ome.example", username="u", password="p"),
        timeout_seconds=5.0,
        bmc_credential=RedfishCredential(name="ome-bmc", username="bu", password="bp"),
        redfish_provider_factory=lambda targets: None,
    )


def _profile(name: str, target: str, target_id: int) -> dict[str, Any]:
    return {
        "Id": 9000 + target_id,
        "ProfileName": name,
        "TargetName": target,
        "TargetId": target_id,
        "TemplateName": "RHOCP Worker v4",
        "TemplateId": 412,
    }


def _device(name: str, tag: str, device_id: int) -> dict[str, Any]:
    return {"Id": device_id, "DeviceName": name, "DeviceServiceTag": tag, "Model": "PowerEdge R650"}


FLEET_PROFILES = [
    _profile("ocp-worker-01", "10.0.0.1", 101),
    _profile("ocp-worker-10", "10.0.0.10", 110),
    _profile("ocp-worker-11", "10.0.0.11", 111),
]
FLEET_DEVICES = [
    _device("10.0.0.1", "TAG0001", 101),
    _device("10.0.0.10", "TAG0010", 110),
    _device("10.0.0.11", "TAG0011", 111),
]


async def _discover(
    profiles: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    *,
    serial: str | None = None,
    name: str | None = None,
) -> OmeIdentity | None:
    return await _provider()._discover_one(
        _PrefixOme(profiles, devices),  # ty: ignore[invalid-argument-type]
        ServerIdentity(serial=serial, name=name),
    )


class TestByServiceTag:
    """The primary path: service tag, then the device's name, then its profile."""

    async def test_gets_the_profile_of_the_device_not_its_prefix_neighbours(self) -> None:
        identity = await _discover(FLEET_PROFILES, FLEET_DEVICES, serial="TAG0001")

        assert identity is not None
        assert identity.name == "ocp-worker-01"
        assert identity.serial == "TAG0001"
        assert identity.profile_id == "9101"

    async def test_the_id_join_beats_a_duplicate_name(self) -> None:
        """Two profiles share a TargetName; `TargetId` says which one belongs to this device."""
        profiles = [
            _profile("ocp-worker-01", "10.0.0.1", 101),
            _profile("stale-copy", "10.0.0.1", 555),
        ]
        identity = await _discover(profiles, FLEET_DEVICES, serial="TAG0001")

        assert identity is not None
        assert identity.name == "ocp-worker-01"

    async def test_a_profile_bound_to_a_different_device_id_is_refused(self) -> None:
        profiles = [_profile("ocp-worker-01", "10.0.0.1", 999), *FLEET_PROFILES[1:]]

        assert await _discover(profiles, FLEET_DEVICES, serial="TAG0001") is None

    async def test_two_exact_names_and_no_id_match_is_refused_not_guessed(self) -> None:
        profiles = [_profile("one", "10.0.0.1", 555), _profile("two", "10.0.0.1", 556)]

        assert await _discover(profiles, FLEET_DEVICES, serial="TAG0001") is None

    async def test_only_prefix_neighbours_means_not_found(self) -> None:
        """OME has profiles for 10.0.0.10 and .11 but none for 10.0.0.1: never use one of them."""
        identity = await _discover(FLEET_PROFILES[1:], FLEET_DEVICES, serial="TAG0001")

        assert identity is None

    async def test_a_longer_service_tag_is_not_the_device(self) -> None:
        devices = [_device("10.0.0.9", "ABC1234", 109)]

        assert await _discover([_profile("p", "10.0.0.9", 109)], devices, serial="ABC123") is None


class TestByProfileName:
    """The fallback when only the server name is known (no service tag)."""

    async def test_gets_the_exact_profile_name_not_its_prefix_neighbours(self) -> None:
        identity = await _discover(FLEET_PROFILES, FLEET_DEVICES, name="ocp-worker-01")

        assert identity is not None
        assert identity.name == "ocp-worker-01"
        assert identity.serial == "TAG0001"

    async def test_the_device_is_the_one_the_profile_targets(self) -> None:
        identity = await _discover(FLEET_PROFILES, FLEET_DEVICES, name="ocp-worker-01")

        assert identity is not None
        assert identity.serial == "TAG0001"
        assert identity.device_id == "101"

    async def test_a_profile_bound_to_another_device_is_not_accepted_by_name(self) -> None:
        """The profile named here targets device 999; the only device of its TargetName is 101."""
        profiles = [_profile("ocp-worker-01", "10.0.0.1", 999)]

        assert await _discover(profiles, FLEET_DEVICES[:1], name="ocp-worker-01") is None

    async def test_a_profile_with_no_target_id_is_accepted_by_exact_name(self) -> None:
        profiles = [_profile("ocp-worker-01", "10.0.0.1", 0)]
        identity = await _discover(profiles, FLEET_DEVICES, serial="TAG0001")

        assert identity is not None
        assert identity.name == "ocp-worker-01"

    async def test_two_devices_with_one_service_tag_are_refused(self) -> None:
        devices = [_device("10.0.0.2", "TAG0001", 102), _device("10.0.0.1", "TAG0001", 101)]

        assert await _discover(FLEET_PROFILES, devices, serial="TAG0001") is None

    async def test_a_name_that_only_prefixes_other_profiles_is_not_found(self) -> None:
        assert await _discover(FLEET_PROFILES, FLEET_DEVICES, name="ocp-worker-1") is None

    async def test_a_profile_of_a_different_device_is_not_attached_to_the_tagged_one(self) -> None:
        """The tag finds device 10.0.0.1, but the profile named here targets 10.0.0.10."""
        profiles = [_profile("ocp-worker-10", "10.0.0.10", 110)]
        identity = await _discover(profiles, FLEET_DEVICES, serial="TAG0001", name="ocp-worker-10")

        assert identity is None

    async def test_a_quote_in_a_name_is_escaped_and_still_matched_exactly(self) -> None:
        profiles = [_profile("o'brien", "10.0.0.5", 105), _profile("o'brien-2", "10.0.0.6", 106)]
        identity = await _discover(profiles, [_device("10.0.0.5", "TAG5", 105)], name="o'brien")

        assert identity is not None
        assert identity.name == "o'brien"


class TestAmbiguityAndIds:
    """Ambiguity is a refusal that no fallback revives; ids tolerate padding."""

    async def test_an_id_match_with_another_profile_name_target_is_not_used(self) -> None:
        profiles = [_profile("sneaky", "10.0.0.10", 101)]

        assert await _discover(profiles, FLEET_DEVICES, serial="TAG0001") is None

    async def test_two_profiles_of_one_device_stay_refused_even_when_named(self) -> None:
        profiles = [_profile("one", "10.0.0.1", 101), _profile("two", "10.0.0.1", 101)]

        assert await _discover(profiles, FLEET_DEVICES, serial="TAG0001", name="one") is None

    async def test_a_duplicate_service_tag_stays_refused_even_when_named(self) -> None:
        devices = [_device("10.0.0.1", "TAG0001", 101), _device("10.0.0.2", "TAG0001", 102)]
        profiles = [_profile("ocp-worker-01", "10.0.0.1", 101)]

        assert await _discover(profiles, devices, serial="TAG0001", name="ocp-worker-01") is None

    async def test_a_padded_target_id_still_joins(self) -> None:
        profiles = [{**_profile("ocp-worker-01", "10.0.0.1", 101), "TargetId": " 101 "}]
        identity = await _discover(profiles, FLEET_DEVICES, serial="TAG0001")

        assert identity is not None
        assert identity.name == "ocp-worker-01"


class TestServiceTagCase:
    """OME may store a tag in another case than the one asked for."""

    async def test_a_tag_in_another_case_still_resolves(self) -> None:
        identity = await _discover(FLEET_PROFILES, FLEET_DEVICES, serial="tag0001")

        assert identity is not None
        assert identity.name == "ocp-worker-01"
