"""The Dell collector: OME names the servers, Redfish measures them.

The seam under test is the join. Both halves are faked — an OME client
returning canned profile/device JSON, and a stand-in for the Redfish pass
returning canned `ProviderServer`s — because what can actually break here
is the correlation between them, not either vendor's own mapping.

See docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, Self

import pytest

from app.domain.enums import ManagerType, Vendor
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.openmanage.provider import OpenManageProvider
from app.infrastructure.providers.redfish.targets import RedfishCredential, RedfishTarget

pytestmark = pytest.mark.asyncio


def _manager() -> Manager:
    """
    The manager every run in this module reports under.

    Returns:
        Manager: An OPENMANAGE manager pointing at a fake appliance.
    """
    return Manager(
        _id="mgr-ome-1",
        name="ome-1",
        type=ManagerType.OPENMANAGE,
        endpoint="ome.example",
        audit=AuditFields.new(),
    )


def _profile(name: str, idrac: str, *, template: str = "RHOCP Worker v4") -> dict[str, Any]:
    """
    One `/ProfileService/Profiles` entry.

    Args:
        name (str): The profile name, which becomes the server's name.
        idrac (str): The profile's `TargetName`, i.e. its iDRAC address.
        template (str): The deployment template name.

    Returns:
        dict[str, Any]: The profile as OME reports it.
    """
    return {
        "ProfileName": name,
        "TargetName": idrac,
        "TemplateName": template,
        "TemplateId": 412,
    }


def _device(
    idrac: str,
    *,
    service_tag: str,
    model: str = "PowerEdge R650",
    network_address: str | None = None,
) -> dict[str, Any]:
    """
    One `/DeviceService/Devices` entry.

    Args:
        idrac (str): The device's `DeviceName`, joined to a profile by it.
        service_tag (str): The Dell service tag.
        model (str): The device model.
        network_address (str | None): `DeviceManagement[0].NetworkAddress`,
            when the entry should carry one.

    Returns:
        dict[str, Any]: The device as OME reports it.
    """
    device: dict[str, Any] = {"DeviceName": idrac, "DeviceServiceTag": service_tag, "Model": model}
    if network_address is not None:
        device["DeviceManagement"] = [{"NetworkAddress": network_address}]
    return device


class _FakeOmeClient:
    """Stands in for `OmeClient`, answering the two bulk calls from canned
    JSON and recording that it was used as a context manager.
    """

    def __init__(self, profiles: list[dict[str, Any]], devices: list[dict[str, Any]]) -> None:
        """
        Args:
            profiles (list[dict[str, Any]]): `/ProfileService/Profiles`.
            devices (list[dict[str, Any]]): `/DeviceService/Devices`.
        """
        self._profiles = profiles
        self._devices = devices
        self.paths: list[str] = []

    async def __aenter__(self) -> Self:
        """
        Returns:
            Self: The logged-in client.
        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """Log out. No-op for the fake."""
        return

    async def get_all(self, path: str) -> list[dict[str, Any]]:
        """
        Answer one bulk enumeration call.

        Args:
            path (str): The OME collection path.

        Returns:
            list[dict[str, Any]]: The canned entries for that path.
        """
        self.paths.append(path)
        return self._profiles if "Profile" in path else self._devices


class _FakeRedfish:
    """Stands in for the Redfish pass: records the targets it was built
    with and yields one canned server per target.
    """

    def __init__(self, targets: list[RedfishTarget], servers: list[ProviderServer]) -> None:
        """
        Args:
            targets (list[RedfishTarget]): What the Dell collector discovered.
            servers (list[ProviderServer]): What to yield for them.
        """
        self.targets = targets
        self._servers = servers
        self.collection_errors: tuple[str, ...] = ()

    async def collect(self) -> AsyncIterator[ProviderServer]:
        """
        Yields:
            ProviderServer: Each canned server in order.
        """
        for server in self._servers:
            yield server


def _collected(host: str, **overrides: Any) -> ProviderServer:
    """
    A server as the Redfish pass would report it, before the OME join.

    Args:
        host (str): The BMC this system was collected from.
        **overrides (Any): Fields to replace on the result.

    Returns:
        ProviderServer: The Redfish half of a collected Dell server.
    """
    defaults: dict[str, Any] = {
        "external_id": f"redfish://{host}/redfish/v1/Systems/System.Embedded.1",
        "vendor": Vendor.DELL.value,
        "name": "ocp4-nyc-prod-worker-03",
        "model": "PowerEdge R650",
        "serial": "7XKD9P3",
        # The shape the Redfish mapping emits, not a bare `https://host`.
        "bmc_address_raw": f"redfish://{host}/redfish/v1/Systems/System.Embedded.1",
        "cpu_sockets": 2,
        "cpu_cores": 32,
        "cpu_threads": 64,
        "storage_total_bytes": 4_320_000_000_000,
    }
    return ProviderServer(**{**defaults, **overrides})


def _provider(
    *,
    profiles: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    servers: list[ProviderServer],
    name_pattern: str = "",
) -> tuple[OpenManageProvider, dict[str, Any]]:
    """
    Build a provider wired to both fakes.

    Args:
        profiles (list[dict[str, Any]]): Canned OME profiles.
        devices (list[dict[str, Any]]): Canned OME devices.
        servers (list[ProviderServer]): What the Redfish pass reports.
        name_pattern (str): The pre-filter to apply.

    Returns:
        tuple[OpenManageProvider, dict[str, Any]]: The provider, and a dict
            the Redfish fake is recorded into as `"redfish"`.
    """
    recorded: dict[str, Any] = {}

    def factory(targets: list[RedfishTarget]) -> Any:
        recorded["redfish"] = _FakeRedfish(targets, servers)
        return recorded["redfish"]

    provider = OpenManageProvider(
        manager=_manager(),
        credentials=ManagerConnection(endpoint="ome.example", username="u", password="p"),
        timeout_seconds=5.0,
        bmc_credential=RedfishCredential(name="ome-bmc", username="bu", password="bp"),
        redfish_provider_factory=factory,
        name_pattern=name_pattern,
    )
    provider._new_client = lambda: _FakeOmeClient(profiles, devices)  # ty: ignore[invalid-assignment]
    return provider, recorded


class TestDiscovery:
    """What OME is asked, and which servers survive to cost a BMC session."""

    async def test_enumerates_with_two_bulk_calls(self) -> None:
        """Two calls regardless of fleet size — the whole point of using OME
        for discovery rather than walking devices.
        """
        provider, _ = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[_collected("10.0.0.1")],
        )
        client = _FakeOmeClient([], [])
        provider._new_client = lambda: client  # ty: ignore[invalid-assignment]
        [server async for server in provider.collect()]
        assert client.paths == ["/ProfileService/Profiles", "/DeviceService/Devices"]

    async def test_the_name_filter_runs_before_any_bmc_is_contacted(self) -> None:
        """The expensive pass is per-server here, so a non-matching profile
        must never become a target — unlike standalone Redfish, where a BMC
        does not know the server's name (docs/dell-collectors.md).
        """
        provider, recorded = _provider(
            profiles=[
                _profile("ocp4-nyc-prod-worker-03", "10.0.0.1"),
                _profile("vmhost-two-14", "10.0.0.2"),
            ],
            devices=[
                _device("10.0.0.1", service_tag="7XKD9P3"),
                _device("10.0.0.2", service_tag="ZZZZZZZ"),
            ],
            servers=[_collected("10.0.0.1")],
            name_pattern="^ocp",
        )
        [server async for server in provider.collect()]
        assert [t.host for t in recorded["redfish"].targets] == ["10.0.0.1"]

    async def test_an_undeployed_profile_is_skipped_without_failing_the_run(self) -> None:
        """A profile with no target device has no server behind it. Counting
        it as unreachable would exit 3 on every run of a healthy estate.
        """
        provider, recorded = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "")],
            devices=[],
            servers=[],
        )
        assert [server async for server in provider.collect()] == []
        assert recorded == {} or recorded["redfish"].targets == []
        assert provider.collection_errors == ()

    async def test_the_profile_name_reaches_the_target(self) -> None:
        """`RedfishTarget.name` becomes `override_name`, which is what keeps
        a Dell server named the thing site parsing needs rather than
        whatever iDRAC calls it.
        """
        provider, recorded = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[_collected("10.0.0.1")],
        )
        [server async for server in provider.collect()]
        target = recorded["redfish"].targets[0]
        assert target.name == "ocp4-nyc-prod-worker-03"
        assert target.credential.username == "bu"


class TestTheJoin:
    """Putting OME's identity back onto what the BMC measured."""

    async def test_the_spt_comes_from_ome(self) -> None:
        """Redfish has no concept of a deployment template, so this field
        exists only because the two halves are joined.
        """
        provider, _ = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[_collected("10.0.0.1")],
        )
        [server] = [s async for s in provider.collect()]
        assert server.profile_template_name == "RHOCP Worker v4"
        assert server.profile_template_external_id == "412"

    async def test_the_bmc_address_keeps_its_metal3_shape(self) -> None:
        """The Redfish pass reports `https://<host>`, but a Dell server's
        address must stay the `idrac-virtualmedia://` form a Metal3
        BareMetalHost round-trips (docs/dell-collectors.md).
        """
        provider, _ = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[_collected("10.0.0.1")],
        )
        [server] = [s async for s in provider.collect()]
        assert server.bmc_address_raw == (
            "idrac-virtualmedia://10.0.0.1/redfish/v1/Systems/System.Embedded.1"
        )

    async def test_measured_hardware_always_wins(self) -> None:
        """The whole reason for the architecture: what the BMC reports is
        not overwritten by anything OME says.
        """
        provider, _ = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="OME-TAG", model="OME Model")],
            servers=[_collected("10.0.0.1", serial="BMC-TAG", model="BMC Model")],
        )
        [server] = [s async for s in provider.collect()]
        assert server.serial == "BMC-TAG"
        assert server.model == "BMC Model"
        assert server.cpu_threads == 64
        assert server.storage_total_bytes == 4_320_000_000_000

    async def test_ome_fills_only_what_the_bmc_left_empty(self) -> None:
        """A BMC that reports no model still yields a modelled server,
        because OME knows it.
        """
        provider, _ = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3", model="PowerEdge R650")],
            servers=[_collected("10.0.0.1", model=None, serial=None)],
        )
        [server] = [s async for s in provider.collect()]
        assert server.model == "PowerEdge R650"
        assert server.serial == "7XKD9P3"

    async def test_several_systems_behind_one_bmc_all_get_the_identity(self) -> None:
        """Joined on the BMC host, not the name, so a chassis reporting more
        than one system does not leave the second one unidentified.
        """
        provider, _ = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[
                _collected("10.0.0.1", external_id="sys-1"),
                _collected("10.0.0.1", external_id="sys-2"),
            ],
        )
        servers = [s async for s in provider.collect()]
        assert [s.profile_template_name for s in servers] == ["RHOCP Worker v4"] * 2

    async def test_the_real_ip_wins_over_a_hostname_targetname(self) -> None:
        """Reproduces a live failure: `TargetName`/`DeviceName` can hold an
        OS hostname, not an address — see docs/dell-collectors.md's
        "Collection flow", 2026-09-10 update.
        """
        provider, recorded = _provider(
            profiles=[_profile("ocp4-compute-five-01", "ocp4-compute-five-01")],
            devices=[
                _device(
                    "ocp4-compute-five-01",
                    service_tag="7XKD9P3",
                    network_address="10.0.0.9",
                )
            ],
            servers=[_collected("10.0.0.9")],
        )
        [server] = [s async for s in provider.collect()]
        assert recorded["redfish"].targets[0].host == "10.0.0.9"
        assert server.bmc_address_raw == (
            "idrac-virtualmedia://10.0.0.9/redfish/v1/Systems/System.Embedded.1"
        )

    async def test_falls_back_to_the_display_name_with_no_network_address(self) -> None:
        """`DeviceManagement` is what a live appliance has always been
        expected to carry, but this must keep working if it is ever absent.
        """
        provider, recorded = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[_collected("10.0.0.1")],
        )
        [server async for server in provider.collect()]
        assert recorded["redfish"].targets[0].host == "10.0.0.1"

    async def test_the_manager_id_is_this_run_s(self) -> None:
        """The Redfish pass is constructed with the same manager, but the
        join asserts it rather than trusting the factory to have done so.
        """
        provider, _ = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[_collected("10.0.0.1", manager_id="something-else")],
        )
        [server] = [s async for s in provider.collect()]
        assert server.manager_id == "mgr-ome-1"


class TestPartialRuns:
    """A run that reached OME but not every BMC must say so."""

    async def test_redfish_errors_are_carried_through(self) -> None:
        """`tools.run_collector` reads `collection_errors` to report PARTIAL.
        Losing the Redfish pass's own per-host failures here would report a
        complete success over a fleet half of which was unreachable.
        """
        provider, recorded = _provider(
            profiles=[_profile("ocp4-nyc-prod-worker-03", "10.0.0.1")],
            devices=[_device("10.0.0.1", service_tag="7XKD9P3")],
            servers=[_collected("10.0.0.1")],
        )
        servers = []
        async for server in provider.collect():
            recorded["redfish"].collection_errors = ("10.0.0.2: unreachable",)
            servers.append(server)
        assert provider.collection_errors == ("10.0.0.2: unreachable",)

    async def test_redfish_errors_survive_a_consumer_that_stops_early(self) -> None:
        """A consumer that stops early (`--limit`, a killed run) throws
        `GeneratorExit` in at this provider's `yield`; the merge used to be
        skipped then, silently dropping every per-host Redfish failure.
        """
        provider, recorded = _provider(
            profiles=[
                _profile("ocp4-nyc-prod-worker-03", "10.0.0.1"),
                _profile("ocp4-nyc-prod-worker-04", "10.0.0.2"),
            ],
            devices=[
                _device("10.0.0.1", service_tag="7XKD9P3"),
                _device("10.0.0.2", service_tag="7XKD9P4"),
            ],
            servers=[_collected("10.0.0.1"), _collected("10.0.0.2")],
        )

        # `aclosing`, not a bare `async for ... break`: `break` alone never
        # calls `.aclose()`, so it would not exercise the `GeneratorExit`
        # path this test is about.
        async with contextlib.aclosing(provider.collect()) as servers:
            async for _server in servers:
                recorded["redfish"].collection_errors = ("10.0.0.3: unreachable",)
                break  # stop before the Redfish pass's own generator exhausts

        assert provider.collection_errors == ("10.0.0.3: unreachable",)


class TestUnreachableHosts:
    """A profile OME knows whose iDRAC did not answer this run."""

    async def test_a_plain_connection_failure_becomes_a_placeholder(self) -> None:
        """The case this collector treats as "the server is down", not a
        run-wide problem — see docs/dell-collectors.md's "Collection
        flow", 2026-09-10 update.
        """
        provider, recorded = _provider(
            profiles=[
                _profile("ocp4-nyc-prod-worker-03", "10.0.0.1"),
                _profile("ocp4-nyc-prod-worker-09", "10.0.0.9"),
            ],
            devices=[
                _device("10.0.0.1", service_tag="7XKD9P3"),
                _device("10.0.0.9", service_tag="7XKD9P9"),
            ],
            servers=[_collected("10.0.0.1")],
        )
        servers = []
        async for server in provider.collect():
            recorded["redfish"].collection_errors = ("10.0.0.9: unreachable — Connection refused",)
            servers.append(server)

        assert provider.collection_errors == ()
        [placeholder] = [s for s in servers if s.external_id == "ome-unreachable:10.0.0.9"]
        assert placeholder.reachable is False
        assert placeholder.serial == "7XKD9P9"
        assert placeholder.name == "ocp4-nyc-prod-worker-09"

    async def test_a_rejected_login_becomes_a_placeholder_too(self) -> None:
        """Widened 2026-09-12: an iDRAC that rejects its credential leaves
        the server exactly as unmeasured as a dead one, and OME already
        knows who it is — so it stays visible rather than vanishing.
        """
        provider, recorded = _provider(
            profiles=[
                _profile("ocp4-nyc-prod-worker-03", "10.0.0.1"),
                _profile("ocp4-nyc-prod-worker-09", "10.0.0.9"),
            ],
            devices=[
                _device("10.0.0.1", service_tag="7XKD9P3"),
                _device("10.0.0.9", service_tag="7XKD9P9"),
            ],
            servers=[_collected("10.0.0.1")],
        )
        servers = []
        async for server in provider.collect():
            recorded["redfish"].collection_errors = (
                "10.0.0.9: login failed for credential 'ome-bmc'",
            )
            servers.append(server)

        assert provider.collection_errors == ()
        [placeholder] = [s for s in servers if s.external_id == "ome-unreachable:10.0.0.9"]
        assert placeholder.reachable is False
        assert placeholder.serial == "7XKD9P9"

    async def test_every_other_failure_still_counts_toward_partial(self) -> None:
        """TLS and a per-host budget are not "the server is down" — they
        may affect many hosts, so they keep today's behaviour: no
        placeholder, still PARTIAL.
        """
        provider, recorded = _provider(
            profiles=[
                _profile("ocp4-nyc-prod-worker-03", "10.0.0.1"),
                _profile("ocp4-nyc-prod-worker-09", "10.0.0.9"),
            ],
            devices=[
                _device("10.0.0.1", service_tag="7XKD9P3"),
                _device("10.0.0.9", service_tag="7XKD9P9"),
            ],
            servers=[_collected("10.0.0.1")],
        )
        servers = []
        async for server in provider.collect():
            recorded["redfish"].collection_errors = (
                "10.0.0.9: TLS verification failed — bad cert",
            )
            servers.append(server)

        assert provider.collection_errors == ("10.0.0.9: TLS verification failed — bad cert",)
        assert all(s.external_id != "ome-unreachable:10.0.0.9" for s in servers)
