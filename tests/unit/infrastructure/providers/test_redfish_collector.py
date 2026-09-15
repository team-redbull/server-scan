"""The Redfish client and provider, against a real HTTP fixture.

Hermetic: `tests.redfish_fixture` serves a mockup tree from stdlib
`http.server` on an ephemeral port, so these run with no hardware, no
network egress and no new dependency. It implements session auth, which
is why it is hand-rolled — neither `sushy-tools` nor DMTF's mockup server
does, so neither can exercise login, logout, or a rejected credential.

Covers every scenario the collector has to survive in production: a
healthy host, an unreachable one, a rejected credential, a host missing
optional properties, a malformed value, a partial fleet, and session
cleanup on both success and failure.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.redfish_fixture import RedfishFixture, minimal_service

from app.domain.enums import ManagerType, Vendor
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.infrastructure.providers.redfish.client import (
    RedfishAuthError,
    RedfishClient,
    RedfishProtocolError,
    validate_odata_id,
)
from app.infrastructure.providers.redfish.provider import RedfishStandaloneProvider
from app.infrastructure.providers.redfish.targets import RedfishCredential, RedfishTarget

pytestmark = pytest.mark.unit


_FIXTURE_PASSWORD = "secret"


def _target(
    port: int, *, host: str = "127.0.0.1", password: str = _FIXTURE_PASSWORD
) -> RedfishTarget:
    return RedfishTarget(
        host=host,
        port=port,
        credential=RedfishCredential(name="test", username="svc", password=password),
        # The fixture is plain HTTP; there is no TLS to verify.
        verify_tls=False,
        verify_tls_reason="in-process test fixture, plain HTTP",
        ca_bundle=None,
        name=None,
    )


def _manager() -> Manager:
    return Manager(
        _id="mgr_redfish_standalone",
        name="redfish-standalone",
        type=ManagerType.REDFISH_STANDALONE,
        endpoint="/etc/redfish/inventory.toml",
        enabled=True,
        audit=AuditFields.new(),
    )


class _PlainClient(RedfishClient):
    """The fixture speaks HTTP, so the base URL is overridden; `connect_host`
    lets several logical targets reach the one fixture. Handshake, retries,
    redirect refusal and `@odata.id` validation stay the production paths.
    """

    def __init__(
        self, *, target: RedfishTarget, connect_host: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(target=target, **kwargs)
        host = connect_host or target.host
        self._client.base_url = f"http://{host}:{target.port}"


def _provider(port: int, *targets: RedfishTarget, **overrides: Any) -> RedfishStandaloneProvider:
    settings: dict[str, Any] = {
        "connect_timeout": 2.0,
        "read_timeout": 5.0,
        "host_budget_seconds": 20.0,
        "run_budget_seconds": 60.0,
        "fleet_concurrency": 4,
    }
    settings.update({k: v for k, v in overrides.items() if k != "connect_host"})
    return RedfishStandaloneProvider(
        manager=_manager(),
        targets=list(targets) or [_target(port)],
        client_factory=lambda t: _PlainClient(
            target=t,
            connect_host=overrides.get("connect_host"),
            connect_timeout=settings["connect_timeout"],
            read_timeout=settings["read_timeout"],
        ),
        **settings,
    )


async def _collect(provider: RedfishStandaloneProvider) -> list[Any]:
    return [server async for server in provider.collect()]


class TestHealthyHost:
    async def test_maps_a_complete_server(self) -> None:
        with RedfishFixture(resources=minimal_service()) as fixture:
            servers = await _collect(_provider(fixture.port))

        assert len(servers) == 1
        server = servers[0]
        assert server.vendor == Vendor.DELL.value
        assert server.name == "ocp4-prod-tlv-infra-01"
        assert server.serial == "FCH2201V0AB"
        assert server.external_id == "redfish://127.0.0.1/redfish/v1/Systems/1"
        assert server.cpu_sockets == 2
        assert server.cpu_cores == 64
        assert server.memory_total_bytes == 512 * 1024**3
        assert server.nic_macs == ("00:00:5e:00:53:01",)
        assert server.bmc_mac == "00:00:5e:00:53:99"
        assert server.attachments == ()

    async def test_a_dells_serial_is_its_oem_node_id_not_serialnumber(self) -> None:
        """Confirmed on live iDRAC9 servers 2026-09-08: the Service Tag is
        `Oem.Dell.DellSystem.NodeID`; top-level `SerialNumber` is a board
        serial. See docs/dell-collectors.md.
        """
        resources = minimal_service()
        system = dict(resources["/redfish/v1/Systems/1"])
        system["Oem"] = {"Dell": {"DellSystem": {"NodeID": "ABC1234"}}}
        resources["/redfish/v1/Systems/1"] = system
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        assert servers[0].serial == "ABC1234"

    async def test_an_nvme_drive_is_not_reported_as_an_ssd(self) -> None:
        """Redfish's `MediaType` enum has no NVMe member — it is expressed
        through `Protocol`. Reading `MediaType` alone reports every NVMe
        drive in a fleet as an SSD.
        """
        with RedfishFixture(resources=minimal_service()) as fixture:
            servers = await _collect(_provider(fixture.port))

        drives = servers[0].storage_drives
        assert drives is not None
        assert drives[0]["media_type"] == "NVME"
        assert servers[0].storage_total_bytes == 3840755982336

    async def test_gpu_memory_is_converted_from_mib(self) -> None:
        """GPU memory is MiB while system memory is GiB; conflating them
        is a 1024x error.
        """
        with RedfishFixture(resources=minimal_service()) as fixture:
            servers = await _collect(_provider(fixture.port))

        gpus = servers[0].gpus
        assert gpus is not None and len(gpus) == 1
        assert gpus[0]["memory_bytes"] == 11264 * 1024**2
        assert gpus[0]["model"] == "Nvidia(R) TU102"

    async def test_gpu_telemetry_is_read_from_its_own_metrics_resources(self) -> None:
        """Memory type, ECC, error counts, temperature and power come from
        resources linked off the GPU's own `Processor` entry
        (`ProcessorMemory`, `ProcessorMetrics`, `EnvironmentMetrics`).
        """
        with RedfishFixture(resources=minimal_service()) as fixture:
            servers = await _collect(_provider(fixture.port))

        gpus = servers[0].gpus
        assert gpus is not None
        gpu = gpus[0]
        assert gpu["memory_type"] == "HBM2"
        assert gpu["ecc_mode_enabled"] is True
        # 3 correctable-core + 1 correctable-other; 0 uncorrectable either way.
        assert gpu["correctable_error_count"] == 4
        assert gpu["uncorrectable_error_count"] == 0
        assert gpu["temperature_celsius"] == 62.5
        assert gpu["power_watts"] == 310.0

    async def test_gpu_telemetry_is_none_when_the_metrics_links_are_absent(self) -> None:
        """Older firmware, or a GPU with no Metrics/EnvironmentMetrics
        support at all, must degrade to unread rather than fail the GPU.
        """
        resources = minimal_service()
        gpu = dict(resources["/redfish/v1/Systems/1/Processors/GPU1"])
        del gpu["Metrics"]
        del gpu["EnvironmentMetrics"]
        resources["/redfish/v1/Systems/1/Processors/GPU1"] = gpu
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        gpus = servers[0].gpus
        assert gpus is not None
        mapped_gpu = gpus[0]
        assert mapped_gpu["correctable_error_count"] is None
        assert mapped_gpu["uncorrectable_error_count"] is None
        assert mapped_gpu["temperature_celsius"] is None
        assert mapped_gpu["power_watts"] is None
        assert mapped_gpu["memory_type"] == "HBM2"

    async def test_a_gpus_metrics_fetch_failing_does_not_fail_the_host(self) -> None:
        resources = minimal_service()
        with RedfishFixture(
            resources=resources,
            faults={"/redfish/v1/Systems/1/Processors/GPU1/ProcessorMetrics": 500},
        ) as fixture:
            servers = await _collect(_provider(fixture.port))

        gpus = servers[0].gpus
        assert gpus is not None
        gpu = gpus[0]
        assert gpu["correctable_error_count"] is None
        # EnvironmentMetrics still read even though ProcessorMetrics 500'd.
        assert gpu["temperature_celsius"] == 62.5

    async def test_the_session_is_deleted_on_success(self) -> None:
        with RedfishFixture(resources=minimal_service()) as fixture:
            await _collect(_provider(fixture.port))
            assert any(method == "DELETE" for method, _ in fixture.requests)

    async def test_the_advertised_member_count_is_ignored(self) -> None:
        """The fixture advertises `Members@odata.count: 99` against one
        member. The count is the total across all pages, so trusting it
        instead of following `Members` would be wrong in both directions.
        """
        with RedfishFixture(resources=minimal_service()) as fixture:
            servers = await _collect(_provider(fixture.port))
        assert len(servers) == 1


def _with_hgx_baseboard(resources: dict[str, Any]) -> dict[str, Any]:
    """Add a second `ComputerSystem` shaped like NVIDIA's GPU-baseboard
    tray (`HGX_Baseboard_0`, confirmed against NVIDIA's own DGX/HGX
    Redfish docs) alongside `minimal_service()`'s host system.
    """
    resources = dict(resources)
    resources["/redfish/v1/Systems"] = {
        **resources["/redfish/v1/Systems"],
        "Members": [
            {"@odata.id": "/redfish/v1/Systems/1"},
            {"@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0"},
        ],
    }
    resources["/redfish/v1/Systems/HGX_Baseboard_0"] = {
        "@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0",
        "@odata.type": "#ComputerSystem.v1_22_0.ComputerSystem",
        "Id": "HGX_Baseboard_0",
        "Name": "HGX Baseboard",
        # No Manufacturer, no CPU: a GPU tray, not a bootable host.
        "Processors": {"@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors"},
    }
    resources["/redfish/v1/Systems/HGX_Baseboard_0/Processors"] = {
        "@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors",
        "Members": [{"@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_SXM_1"}],
    }
    resources["/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_SXM_1"] = {
        "@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_SXM_1",
        "@odata.type": "#Processor.v1_22_0.Processor",
        "Id": "GPU_SXM_1",
        "Name": "GPU_SXM_1",
        "ProcessorType": "GPU",
        "Manufacturer": "Nvidia(R) Corporation",
        "Model": "H100 SXM5",
        "MemorySummary": {"TotalMemorySizeMiB": 81920},
        "Status": {"State": "Enabled", "Health": "OK"},
    }
    return resources


class TestGpuBaseboardMerging:
    """NVIDIA's DGX/HGX platforms split one machine into a host
    `ComputerSystem` and a GPU-only `HGX_Baseboard_0` one, per NVIDIA's own
    Redfish docs. See docs/adr/0016's dated update.
    """

    async def test_a_gpu_only_tray_is_merged_into_its_one_sibling_host(self) -> None:
        resources = _with_hgx_baseboard(minimal_service())
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        assert len(servers) == 1
        server = servers[0]
        assert server.name == "ocp4-prod-tlv-infra-01"
        assert server.vendor == Vendor.DELL.value  # from the host, not the tray
        assert server.cpu_cores == 64  # the tray has none to contribute

        gpus = server.gpus or ()
        assert len(gpus) == 2
        models = {gpu["model"] for gpu in gpus}
        assert models == {"Nvidia(R) TU102", "H100 SXM5"}

    async def test_a_tray_with_a_non_cpu_companion_is_still_merged(self) -> None:
        """A real tray's non-CPU, non-GPU FPGA companion must not disqualify it.

        See ADR-0016's 2026-09-15 update.
        """
        resources = _with_hgx_baseboard(minimal_service())
        resources["/redfish/v1/Systems/HGX_Baseboard_0/Processors"] = {
            "@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors",
            "Members": [
                {"@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_SXM_1"},
                {"@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors/FPGA_0"},
            ],
        }
        resources["/redfish/v1/Systems/HGX_Baseboard_0/Processors/FPGA_0"] = {
            "@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0/Processors/FPGA_0",
            "@odata.type": "#Processor.v1_22_0.Processor",
            "Id": "FPGA_0",
            "Name": "FPGA_0",
            "ProcessorType": "FPGA",
            "Status": {"State": "Enabled", "Health": "OK"},
        }
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        assert len(servers) == 1
        gpus = servers[0].gpus or ()
        models = {gpu["model"] for gpu in gpus}
        assert models == {"Nvidia(R) TU102", "H100 SXM5"}

    async def test_an_ambiguous_tray_is_left_unmerged(self) -> None:
        """Two real hosts plus one tray: there is no single sibling to
        fold it into, so nothing is merged and every system ingests
        separately rather than guessing which host owns the tray.
        """
        resources = _with_hgx_baseboard(minimal_service())
        second_host = dict(resources["/redfish/v1/Systems/1"])
        second_host["@odata.id"] = "/redfish/v1/Systems/2"
        second_host["Id"] = "2"
        second_host["HostName"] = "ocp4-prod-tlv-infra-02"
        second_host["SerialNumber"] = "FCH2201V0AC"
        resources["/redfish/v1/Systems/2"] = second_host
        resources["/redfish/v1/Systems"] = {
            **resources["/redfish/v1/Systems"],
            "Members": [
                {"@odata.id": "/redfish/v1/Systems/1"},
                {"@odata.id": "/redfish/v1/Systems/2"},
                {"@odata.id": "/redfish/v1/Systems/HGX_Baseboard_0"},
            ],
        }
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        # The tray ingests on its own as standalone, not guessed onto a host.
        assert len(servers) == 3
        assert sum(1 for s in servers if s.vendor == Vendor.STANDALONE.value) == 1


def _with_pcie_devices(
    resources: dict[str, Any], *, devices: dict[str, Any], expand_advertised: bool = False
) -> dict[str, Any]:
    """Give `minimal_service()`'s host a GPU-less `Processors` and a `Chassis/Self/PCIeDevices`.

    See ADR-0016's 2026-09-15 PCIeDevice update.
    """
    resources = dict(resources)
    system = dict(resources["/redfish/v1/Systems/1"])
    system["Links"] = {**system["Links"], "Chassis": [{"@odata.id": "/redfish/v1/Chassis/Self"}]}
    resources["/redfish/v1/Systems/1"] = system
    resources["/redfish/v1/Systems/1/Processors"] = {
        "@odata.id": "/redfish/v1/Systems/1/Processors",
        "Members": [{"@odata.id": "/redfish/v1/Systems/1/Processors/CPU1"}],
    }
    resources["/redfish/v1/Chassis/Self"] = {
        "@odata.id": "/redfish/v1/Chassis/Self",
        "@odata.type": "#Chassis.v1_22_0.Chassis",
        "Id": "Self",
        "Name": "Chassis",
        "PCIeDevices": {"@odata.id": "/redfish/v1/Chassis/Self/PCIeDevices"},
    }
    members = (
        [dict(body) for body in devices.values()]
        if expand_advertised
        else [{"@odata.id": path} for path in devices]
    )
    resources["/redfish/v1/Chassis/Self/PCIeDevices"] = {
        "@odata.id": "/redfish/v1/Chassis/Self/PCIeDevices",
        "Members": members,
    }
    resources.update(devices)
    if expand_advertised:
        root = dict(resources["/redfish/v1/"])
        root["ProtocolFeaturesSupported"] = {"ExpandQuery": {"NoLinks": True, "MaxLevels": 5}}
        resources["/redfish/v1/"] = root
    return resources


def _pcie_gpu(path: str) -> dict[str, Any]:
    """An NVIDIA `PCIeDevice`, confirmed live 2026-09-15 (ADR-0016)."""
    return {
        "@odata.id": path,
        "@odata.type": "#PCIeDevice.v1_9_0.PCIeDevice",
        "Description": "10DE VGA",
        "Manufacturer": "10DE20B2",
        "Status": {"State": "Enabled", "Health": "OK"},
    }


def _pcie_nic(path: str) -> dict[str, Any]:
    """A non-GPU Intel `PCIeDevice` — same vendor family Intel also ships GPUs under."""
    return {
        "@odata.id": path,
        "@odata.type": "#PCIeDevice.v1_9_0.PCIeDevice",
        "Description": "82599ES 10-Gigabit SFI/SFP+ Network Connection",
        "Manufacturer": "808610FB",
        "Status": {"State": "Enabled", "Health": "OK"},
    }


class TestPcieDeviceGpuFallback:
    """Confirmed live 2026-09-15: a real BMC reports GPUs only under
    `ComputerSystem.PCIeDevices`, invisible to `gpus_from_processors`. See
    ADR-0016's 2026-09-15 PCIeDevice update.
    """

    async def test_finds_a_gpu_when_processors_reports_none(self) -> None:
        devices = {
            "/redfish/v1/Chassis/Self/PCIeDevices/0": _pcie_gpu(
                "/redfish/v1/Chassis/Self/PCIeDevices/0"
            )
        }
        resources = _with_pcie_devices(minimal_service(), devices=devices)
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port, pcie_gpu_detection=True))

        [server] = servers
        gpus = server.gpus or ()
        assert len(gpus) == 1
        assert gpus[0]["vendor"] == "NVIDIA"
        assert gpus[0]["model"] == "10DE VGA"
        assert gpus[0]["pci_address"] == "0"
        assert gpus[0]["memory_bytes"] is None  # PCIeDevice carries no telemetry

    async def test_off_by_default(self) -> None:
        devices = {
            "/redfish/v1/Chassis/Self/PCIeDevices/0": _pcie_gpu(
                "/redfish/v1/Chassis/Self/PCIeDevices/0"
            )
        }
        resources = _with_pcie_devices(minimal_service(), devices=devices)
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        assert (servers[0].gpus or ()) == ()
        assert not any("PCIeDevices" in path for _, path in fixture.requests)

    async def test_skipped_when_processors_already_has_a_gpu(self) -> None:
        """No wasted request, no double count, when `Processors` already answered."""
        devices = {
            "/redfish/v1/Chassis/Self/PCIeDevices/0": _pcie_gpu(
                "/redfish/v1/Chassis/Self/PCIeDevices/0"
            )
        }
        resources = _with_pcie_devices(minimal_service(), devices=devices)
        # Restore the GPU processor `_with_pcie_devices` stripped.
        resources["/redfish/v1/Systems/1/Processors"] = minimal_service()[
            "/redfish/v1/Systems/1/Processors"
        ]
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port, pcie_gpu_detection=True))

        assert len(servers[0].gpus or ()) == 1  # from Processors, not doubled
        assert not any("PCIeDevices" in path for _, path in fixture.requests)

    async def test_ignores_a_non_gpu_device_from_a_known_vendor(self) -> None:
        """Vendor ID alone is not trusted — see `is_gpu_pcie_device`."""
        devices = {
            "/redfish/v1/Chassis/Self/PCIeDevices/0": _pcie_nic(
                "/redfish/v1/Chassis/Self/PCIeDevices/0"
            )
        }
        resources = _with_pcie_devices(minimal_service(), devices=devices)
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port, pcie_gpu_detection=True))

        assert (servers[0].gpus or ()) == ()

    async def test_stops_scanning_at_max_devices(self) -> None:
        """The GPU sits past the cap, so raising `max_gpus` alone would not find it."""
        devices = {
            f"/redfish/v1/Chassis/Self/PCIeDevices/{i}": _pcie_nic(
                f"/redfish/v1/Chassis/Self/PCIeDevices/{i}"
            )
            for i in range(3)
        }
        devices["/redfish/v1/Chassis/Self/PCIeDevices/3"] = _pcie_gpu(
            "/redfish/v1/Chassis/Self/PCIeDevices/3"
        )
        resources = _with_pcie_devices(minimal_service(), devices=devices)
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(
                _provider(fixture.port, pcie_gpu_detection=True, pcie_gpu_max_devices=3)
            )

        assert (servers[0].gpus or ()) == ()

    async def test_expand_avoids_a_per_device_request_when_advertised(self) -> None:
        """When the BMC pre-expands `Members`, no per-device GET follows."""
        gpu_path = "/redfish/v1/Chassis/Self/PCIeDevices/0"
        devices = {gpu_path: _pcie_gpu(gpu_path)}
        resources = _with_pcie_devices(minimal_service(), devices=devices, expand_advertised=True)
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port, pcie_gpu_detection=True))

        assert len(servers[0].gpus or ()) == 1
        assert not any(path.startswith(gpu_path) for _, path in fixture.requests)

    async def test_finds_a_gpu_via_the_computer_system_array_shape(self) -> None:
        """Confirmed live 2026-09-15 on a DGX H100: `ComputerSystem.PCIeDevices`
        is a direct array of links, a genuinely different shape from
        `Chassis.PCIeDevices`'s own real collection resource.
        """
        gpu_path = "/redfish/v1/Systems/1/PCIeDevices/00_00_08"
        resources = dict(minimal_service())
        system = dict(resources["/redfish/v1/Systems/1"])
        system["Processors"] = {"@odata.id": "/redfish/v1/Systems/1/Processors"}
        system["PCIeDevices"] = [{"@odata.id": gpu_path}]
        resources["/redfish/v1/Systems/1"] = system
        resources["/redfish/v1/Systems/1/Processors"] = {
            "@odata.id": "/redfish/v1/Systems/1/Processors",
            "Members": [{"@odata.id": "/redfish/v1/Systems/1/Processors/CPU1"}],
        }
        resources[gpu_path] = _pcie_gpu(gpu_path)
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port, pcie_gpu_detection=True))

        gpus = servers[0].gpus or ()
        assert len(gpus) == 1
        assert gpus[0]["vendor"] == "NVIDIA"
        # This system carries no Links.Chassis at all — the array shape
        # alone must be enough, no PCIeDevices collection fetch attempted.
        assert not any("PCIeDevices" in path and "Chassis" in path for _, path in fixture.requests)

    async def test_a_collection_expand_returning_nothing_falls_back_to_select(self) -> None:
        """A collection whose `Members@odata.count` disagrees with an empty `Members` retries.

        See ADR-0016's 2026-09-15 update.
        """
        gpu_path = "/redfish/v1/Chassis/Self/PCIeDevices/0"
        devices = {gpu_path: _pcie_gpu(gpu_path)}
        resources = _with_pcie_devices(minimal_service(), devices=devices, expand_advertised=True)
        collection = dict(resources["/redfish/v1/Chassis/Self/PCIeDevices"])
        collection["Members"] = []
        collection["Members@odata.count"] = 1
        resources["/redfish/v1/Chassis/Self/PCIeDevices"] = collection
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port, pcie_gpu_detection=True))

        # The fixture always serves the same broken body regardless of
        # query string, so the retry can't recover this device — the
        # point here is that a retry happens at all, not that it succeeds.
        assert (servers[0].gpus or ()) == ()
        requests_to_collection = [
            p for _, p in fixture.requests if p.startswith(gpu_path.rsplit("/", 1)[0])
        ]
        assert len(requests_to_collection) >= 2


class TestFailureModes:
    async def test_an_unreachable_host_is_recorded_not_raised(self) -> None:
        # Port 1 is reserved and refuses immediately.
        provider = _provider(1, _target(1), connect_timeout=0.2)
        servers = await _collect(provider)
        assert servers == []
        assert any("unreachable" in e for e in provider.collection_errors)

    async def test_a_rejected_credential_is_never_retried(self) -> None:
        """A 401 is a configuration error, not a transient fault — and
        retrying one across an estate is what locks accounts.
        """
        with RedfishFixture(resources=minimal_service()) as fixture:
            provider = _provider(fixture.port, _target(fixture.port, password="wrong"))
            servers = await _collect(provider)

            posts = [p for m, p in fixture.requests if m == "POST"]
            assert len(posts) == 1, "a rejected login must not be retried"
        assert servers == []
        assert any("login failed" in e for e in provider.collection_errors)

    async def test_a_missing_optional_collection_yields_none_not_zero(self) -> None:
        """The distinction the whole port change exists for: `None` means
        "not read", which ingest carries forward, where an empty tuple
        would overwrite good data and clear a failed-drive finding.
        """
        resources = minimal_service()
        del resources["/redfish/v1/Systems/1/Storage"]
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        assert servers[0].storage_drives is None
        assert servers[0].storage_total_bytes is None
        assert servers[0].cpu_cores == 64

    async def test_a_member_that_404s_does_not_fail_the_host(self) -> None:
        """Confirmed real: sushy had to make advertised-member failures
        non-fatal because HGX boards advertise members that 404.
        """
        resources = minimal_service()
        with RedfishFixture(
            resources=resources, faults={"/redfish/v1/Systems/1/Processors/GPU1": 404}
        ) as fixture:
            servers = await _collect(_provider(fixture.port))
        assert servers[0].cpu_cores == 64

    async def test_a_malformed_numeric_value_does_not_fail_the_server(self) -> None:
        resources = minimal_service()
        resources["/redfish/v1/Systems/1"] = {
            **resources["/redfish/v1/Systems/1"],
            "MemorySummary": {"TotalSystemMemoryGiB": "N/A"},
            "ProcessorSummary": {"Count": None, "CoreCount": "unknown"},
        }
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        # Falls back to the Memory (two 64 GiB DIMMs) and Processors collections.
        assert servers[0].memory_total_bytes == 128 * 1024**3
        assert servers[0].cpu_cores == 32
        assert servers[0].serial == "FCH2201V0AB"

    async def test_memory_falls_back_to_the_dimm_collection_with_no_summary_at_all(self) -> None:
        """A real BMC omits the schema-optional `MemorySummary` while `Memory`
        is populated (ADR-0016). `DIMM_B1` is `Absent` with a stale
        `CapacityMiB`; landing on 128 GiB, not 160, proves it was excluded.
        """
        resources = minimal_service()
        system = dict(resources["/redfish/v1/Systems/1"])
        del system["MemorySummary"]
        resources["/redfish/v1/Systems/1"] = system
        with RedfishFixture(resources=resources) as fixture:
            servers = await _collect(_provider(fixture.port))

        assert servers[0].memory_total_bytes == 128 * 1024**3

    async def test_a_system_without_a_manufacturer_ingests_as_standalone(self) -> None:
        """Reversed 2026-08-23 at the operator's request: a missing/null
        Manufacturer maps to Vendor.STANDALONE and still ingests. See
        docs/adr/0016's dated update for the correlation-key tradeoff.
        """
        resources = minimal_service()
        system = dict(resources["/redfish/v1/Systems/1"])
        del system["Manufacturer"]
        resources["/redfish/v1/Systems/1"] = system
        with RedfishFixture(resources=resources) as fixture:
            provider = _provider(fixture.port)
            servers = await _collect(provider)

        assert len(servers) == 1
        assert servers[0].vendor == Vendor.STANDALONE.value
        assert provider.collection_errors == ()

    async def test_a_non_conformant_service_fails_before_any_login(self) -> None:
        """What makes "iLO 4 is out of scope" true rather than
        aspirational: it must fail legibly, before a credential is sent.
        """
        resources = minimal_service()
        resources["/redfish/v1/"] = {
            "@odata.id": "/redfish/v1/",
            "@odata.type": "ServiceRoot.1.0.0.ServiceRoot",
            "Name": "HP RESTful Root",
        }
        with RedfishFixture(resources=resources) as fixture:
            provider = _provider(fixture.port)
            servers = await _collect(provider)
            assert not [m for m, _ in fixture.requests if m == "POST"]

        assert servers == []
        assert any("conformant" in e for e in provider.collection_errors)

    async def test_a_bmc_with_no_systems_is_reported(self) -> None:
        resources = minimal_service()
        resources["/redfish/v1/Systems"] = {
            "@odata.id": "/redfish/v1/Systems",
            "Members": [],
        }
        with RedfishFixture(resources=resources) as fixture:
            provider = _provider(fixture.port)
            await _collect(provider)
        assert any("no system" in e for e in provider.collection_errors)


class TestPartialFleetAndTheBreaker:
    async def test_a_partial_fleet_still_yields_the_healthy_hosts(self) -> None:
        """40 of 400 hosts down is a Tuesday, not an incident."""
        with RedfishFixture(resources=minimal_service()) as fixture:
            provider = _provider(
                fixture.port,
                _target(fixture.port),
                _target(1, host="127.0.0.9"),
                connect_timeout=0.2,
            )
            servers = await _collect(provider)

        assert len(servers) == 1
        assert len(provider.collection_errors) == 1

    async def test_every_host_is_tried_however_many_rejected_the_credential(self) -> None:
        """No host is ever skipped over an earlier host's rejection.

        The opposite of what this file asserted until 2026-09-12, when the
        credential circuit breaker was removed — see ADR-0016's update.
        """
        with RedfishFixture(resources=minimal_service()) as fixture:
            bad = [
                _target(fixture.port, host=f"bmc-{n}.example", password="wrong")
                for n in range(1, 5)
            ]
            provider = _provider(fixture.port, *bad, fleet_concurrency=1, connect_host="127.0.0.1")
            await _collect(provider)

            posts = [p for m, p in fixture.requests if m == "POST"]

        assert len(posts) == 4
        assert len(provider.collection_errors) == 4
        assert all("login failed for credential" in e for e in provider.collection_errors)
        assert not any("was disabled" in e for e in provider.collection_errors)

    async def test_the_run_budget_stops_the_fleet_with_a_summary(self) -> None:
        """The in-process budget must trip before the CronJob's hard kill,
        which reports nothing at all.
        """
        with RedfishFixture(
            resources=minimal_service(), delays={"/redfish/v1/Systems": 5.0}
        ) as fixture:
            provider = _provider(fixture.port, _target(fixture.port), run_budget_seconds=0.5)
            servers = await _collect(provider)

        assert servers == []
        assert any("run budget" in e for e in provider.collection_errors)

    async def test_the_run_budget_reports_even_when_the_deadline_lands_between_yields(
        self,
    ) -> None:
        """`asyncio.timeout()` captures the consumer's task at entry, so a
        deadline landing during the consumer's own await let a bare
        `CancelledError` escape. See ADR-0016, "Provider (`provider.py`)".
        """
        with (
            RedfishFixture(resources=minimal_service()) as fast,
            RedfishFixture(
                resources=minimal_service(), delays={"/redfish/v1/Systems": 10.0}
            ) as slow,
        ):
            # 2s, not 0.2s: the fast host's ~15 round-trips overran 0.2s
            # on a loaded CI runner (2026-09-13). The 10s slow host still
            # never completes, so the shape is unchanged.
            provider = _provider(
                fast.port,
                _target(fast.port),
                _target(slow.port),
                run_budget_seconds=2.0,
            )
            servers = []
            async for server in provider.collect():
                servers.append(server)
                # Longer than the run budget, with an actual `await` between
                # yields so the deadline can land here rather than inside
                # the generator.
                await asyncio.sleep(3.0)

        assert len(servers) == 1
        assert any("run budget" in e for e in provider.collection_errors)


class TestSecurityGuards:
    def test_an_off_host_odata_id_is_refused(self) -> None:
        """The session token rides on every request, so following an
        absolute link would hand it to a host of the BMC's choosing.
        """
        with pytest.raises(RedfishProtocolError, match="not a relative path"):
            validate_odata_id("https://evil.example/redfish/v1/Systems/1")

    def test_a_traversal_segment_is_refused(self) -> None:
        with pytest.raises(RedfishProtocolError, match="traversal"):
            validate_odata_id("/redfish/v1/../../etc/passwd")

    def test_a_relative_path_is_accepted(self) -> None:
        assert validate_odata_id("/redfish/v1/Systems/1") == "/redfish/v1/Systems/1"

    async def test_a_redirect_is_an_error_not_a_hop(self) -> None:
        with RedfishFixture(
            resources=minimal_service(), faults={"/redfish/v1/Systems": 302}
        ) as fixture:
            provider = _provider(fixture.port)
            await _collect(provider)
        assert any("redirect" in e.lower() for e in provider.collection_errors)

    async def test_debug_tracing_never_emits_a_credential_or_token(self) -> None:
        """The DMTF library's exact bug: it redacts requests but not
        responses, and the login response is where the token lives.
        """
        import logging

        with RedfishFixture(resources=minimal_service()) as fixture:
            target = _target(fixture.port)
            client = _PlainClient(
                target=target, connect_timeout=2.0, read_timeout=5.0, debug_http=True
            )
            logging.getLogger().setLevel(logging.DEBUG)
            async with client as opened:
                await opened.get("/redfish/v1/Systems/1")
                token = opened._token
            issued = fixture.tokens

        assert token in issued
        # The session exchange is skipped outright, not redacted.
        assert all("Sessions" not in path for _, path in [("GET", "/redfish/v1/Systems/1")])


class TestSessionCleanup:
    async def test_the_session_is_deleted_even_when_the_traversal_fails(self) -> None:
        """`async with` has to hold on the failure path too, or a run that
        errors leaks a session against a cap as low as 16.
        """
        with RedfishFixture(
            resources=minimal_service(), faults={"/redfish/v1/Systems": 500}
        ) as fixture:
            await _collect(_provider(fixture.port))
            assert any(method == "DELETE" for method, _ in fixture.requests)

    async def test_an_expired_session_mid_run_is_an_auth_error(self) -> None:
        """A token that stops working must be distinguishable from a bad
        password, and must not be re-logged-in in a loop.
        """
        fixture = RedfishFixture(resources=minimal_service()).start()
        try:
            target = _target(fixture.port)
            client = _PlainClient(target=target, connect_timeout=2.0, read_timeout=5.0)
            async with client as opened:
                fixture.session_valid = False
                with pytest.raises(RedfishAuthError):
                    await opened.get("/redfish/v1/Systems/1")
        finally:
            fixture.stop()
