"""`kubevirt.mapping`: every field of a VM, both its "read, none" and its "unread" (ADR-0058)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.kubevirt_fixture import Vm, kubevirt_tree

from app.infrastructure.providers.kubevirt.mapping import (
    controllers_for,
    cpu_fields,
    serial_of,
    system_to_provider_server,
)

pytestmark = pytest.mark.unit

_NETLOC = "kv.apps.example"


def _members(tree: dict[str, Any], collection: str) -> list[dict[str, Any]]:
    return [tree[m["@odata.id"]] for m in tree[collection]["Members"]]


def _map(vm: Vm, **overrides: Any) -> Any:
    tree = kubevirt_tree(vm)
    p = vm.path
    storage = _members(tree, f"{p}/Storage")
    kwargs: dict[str, Any] = {
        "processors": None,
        "memory": _members(tree, f"{p}/Memory"),
        "storage": storage,
        "drives": [tree[d["@odata.id"]] for s in storage for d in s["Drives"]],
        "interfaces": _members(tree, f"{p}/EthernetInterfaces"),
    }
    kwargs.update(overrides)
    return system_to_provider_server(tree[p], netloc=_NETLOC, manager_id="mgr_kubevirt", **kwargs)


def test_a_running_vm_maps_every_field() -> None:
    vm = Vm(
        name="ocp-tlv-kv-01",
        namespace="vms",
        serial="KV01-SERIAL-0001",
        uuid="5d2f7a1c-3b6e-4c8a-9f10-0a1b2c3d4e01",
        model="q35",
        cpu_model="Icelake-Server-v2",
        vcpus=4,
        memory_mib=8192,
        nics=[("default", "02:5c:00:00:00:01"), ("data", "02:5c:00:00:00:02")],
    )
    server = _map(vm)

    assert server.external_id == f"kubevirt://{_NETLOC}/redfish/v1/Systems/vms.ocp-tlv-kv-01"
    assert server.bmc_address_raw == f"redfish://{_NETLOC}/redfish/v1/Systems/vms.ocp-tlv-kv-01"
    assert server.vendor == "kubevirt"
    assert server.name == "ocp-tlv-kv-01"
    assert server.serial == "KV01-SERIAL-0001"
    assert server.system_uuid == "5d2f7a1c-3b6e-4c8a-9f10-0a1b2c3d4e01"
    assert server.model == "q35"
    assert server.manager_id == "mgr_kubevirt"
    assert server.reachable is True
    assert server.unreachable_reason is None
    assert (server.cpu_sockets, server.cpu_cores, server.cpu_threads) == (1, 4, 4)
    assert server.cpu_model == "Icelake-Server-v2"
    assert server.memory_total_bytes == 8 * 1024**3
    assert server.memory_modules == (
        {
            "slot": "1",
            "size_bytes": 8 * 1024**3,
            "type": "DRAM",
            "speed_mhz": None,
            "serial": None,
            "health": "HEALTHY",
        },
    )
    assert server.nic_macs == ("02:5c:00:00:00:01", "02:5c:00:00:00:02")
    assert [(n.name, n.mac, n.link_state, n.location, n.speed_mbps) for n in server.nics] == [
        ("default", "02:5c:00:00:00:01", "UP", "default", None),
        ("data", "02:5c:00:00:00:02", "UP", "data", None),
    ]


def test_what_a_vm_never_has_is_read_as_none_not_unread() -> None:
    server = _map(Vm(name="ocp-tlv-kv-01"))

    assert server.psus == ()
    assert server.gpus == ()
    assert server.attachments == ()
    assert server.tags == ()
    assert server.profile_template_name == ""
    assert server.profile_template_external_id == ""
    assert server.bmc_mac is None
    assert server.raid_check is None


def test_drives_keep_an_unknown_capacity_out_of_the_total() -> None:
    server = _map(
        Vm(name="v", disks=[("rootdisk", None), ("scratch", 1024**3), ("data", 2 * 1024**3)])
    )

    drives = {d["id"].rsplit("/", 1)[-1]: d for d in server.storage_drives}
    assert drives["rootdisk"]["capacity_bytes"] is None
    assert drives["scratch"]["capacity_bytes"] == 1024**3
    assert all(d["media_type"] == "UNKNOWN" and d["health"] == "HEALTHY" for d in drives.values())
    assert server.storage_total_bytes == 3 * 1024**3
    assert server.storage_controllers == (
        {"id": "1", "name": "VM Disks", "kind": "OTHER", "drive_count": 3, "volumes": ()},
    )


def test_a_vm_with_no_disks_is_read_and_empty() -> None:
    server = _map(Vm(name="v", disks=[]))

    assert server.storage_drives == ()
    assert server.storage_total_bytes == 0
    assert server.storage_controllers == (
        {"id": "1", "name": "VM Disks", "kind": "OTHER", "drive_count": 0, "volumes": ()},
    )


def test_a_vm_with_no_nics_is_read_and_empty() -> None:
    server = _map(Vm(name="v", nics=[]))

    assert server.nic_macs == ()
    assert server.nics == ()


class TestStoppedVm:
    """A stopped VM's NoLink is not a cable fault (ADR-0058)."""

    def test_every_link_is_unknown_and_the_mac_is_kept(self) -> None:
        server = _map(Vm(name="ocp-tlv-kv-03", power="Off"))

        assert server.nic_macs == ("02:00:00:00:00:01",)
        assert [n.link_state for n in server.nics] == ["UNKNOWN"]

    @pytest.mark.parametrize("power", ["Off", "PoweringOn", "Paused", None, ""])
    def test_anything_but_on_is_unknown(self, power: str | None) -> None:
        vm = Vm(name="v")
        server = _map(vm)
        tree = kubevirt_tree(vm)
        system = dict(tree[vm.path], PowerState=power)
        interfaces = [
            tree[m["@odata.id"]] for m in tree[f"{vm.path}/EthernetInterfaces"]["Members"]
        ]
        stopped = system_to_provider_server(
            system,
            netloc=_NETLOC,
            manager_id="mgr_kubevirt",
            processors=None,
            memory=None,
            storage=None,
            drives=None,
            interfaces=interfaces,
        )
        assert [n.link_state for n in server.nics] == ["UP"]
        assert [n.link_state for n in stopped.nics] == ["UNKNOWN"]

    def test_a_running_vm_with_a_disconnected_nic_is_down(self) -> None:
        vm = Vm(name="v")
        tree = kubevirt_tree(vm)
        nic = dict(tree[f"{vm.path}/EthernetInterfaces/default"], LinkStatus="NoLink")
        server = _map(vm, interfaces=[nic])

        assert [n.link_state for n in server.nics] == ["DOWN"]

    def test_the_cpu_model_is_read_and_empty(self) -> None:
        server = _map(Vm(name="v", power="Off", cpu_model=None))

        assert server.cpu_model == ""
        assert server.cpu_cores == 2


class TestSerial:
    def test_the_firmware_serial_wins(self) -> None:
        assert serial_of({"Id": "vms.v", "SerialNumber": " ABC-1 "}) == "ABC-1"

    @pytest.mark.parametrize("raw", [None, "", "   ", "Not Specified", "To Be Filled By O.E.M."])
    def test_a_missing_or_placeholder_serial_falls_back_to_the_enhanced_id(
        self, raw: str | None
    ) -> None:
        assert serial_of({"Id": "vms.ocp-tlv-kv-02", "SerialNumber": raw}) == "vms.ocp-tlv-kv-02"

    def test_an_older_facade_build_without_serial_uses_the_id(self) -> None:
        server = _map(Vm(name="ocp-tlv-kv-02", serial=None, uuid=None, model=None))

        assert server.serial == "vms.ocp-tlv-kv-02"
        assert server.system_uuid is None
        assert server.model is None

    def test_the_legacy_id_is_the_bare_name(self) -> None:
        server = _map(Vm(name="ocp-tlv-kv-02", serial=None, legacy_id=True))

        assert server.serial == "ocp-tlv-kv-02"
        assert server.external_id.endswith("/redfish/v1/Systems/ocp-tlv-kv-02")

    def test_the_odata_id_is_the_last_resort(self) -> None:
        assert serial_of({"@odata.id": "/redfish/v1/Systems/x"}) == "/redfish/v1/Systems/x"


class TestCpu:
    def test_counts_and_model_come_from_the_summary(self) -> None:
        system = {
            "ProcessorSummary": {
                "Count": 2,
                "CoreCount": 8,
                "LogicalProcessorCount": 16,
                "Model": "EPYC",
            }
        }
        assert cpu_fields(system, None) == (2, 8, 16, "EPYC")

    def test_a_summary_without_a_model_is_read_and_empty(self) -> None:
        system = {"ProcessorSummary": {"Count": 1, "CoreCount": 2, "LogicalProcessorCount": 2}}
        assert cpu_fields(system, None) == (1, 2, 2, "")

    def test_no_summary_falls_back_to_the_processors(self) -> None:
        processors = [{"ProcessorType": "CPU", "TotalCores": 4, "TotalThreads": 4, "Model": "host"}]
        assert cpu_fields({}, processors) == (1, 4, 4, "host")

    def test_processors_read_without_a_model_is_empty(self) -> None:
        processors = [{"ProcessorType": "CPU", "TotalCores": 4, "TotalThreads": 4}]
        assert cpu_fields({}, processors) == (1, 4, 4, "")

    def test_nothing_read_is_unread(self) -> None:
        assert cpu_fields({}, None) == (None, None, None, None)

    def test_an_unread_topology_leaves_every_cpu_field_unread(self) -> None:
        server = _map(Vm(name="v", processor_summary=False), processors=None)

        assert (server.cpu_sockets, server.cpu_cores, server.cpu_threads, server.cpu_model) == (
            None,
            None,
            None,
            None,
        )


class TestUnread:
    """A failed sub-resource read is None, never an empty value (the provider contract)."""

    def test_memory_unread(self) -> None:
        server = _map(Vm(name="v", memory_mib=None), memory=None)

        assert server.memory_total_bytes is None
        assert server.memory_modules is None

    def test_memory_from_the_summary_survives_an_unread_collection(self) -> None:
        server = _map(Vm(name="v", memory_mib=1024), memory=None)

        assert server.memory_total_bytes == 1024**3
        assert server.memory_modules is None

    def test_a_memory_module_with_no_capacity_is_unknown_not_zero(self) -> None:
        """A stopped instancetype VM declares no memory in its template: its size is
        unknown, so the stored total is carried forward rather than zeroed."""
        server = _map(Vm(name="v", memory_mib=None))

        assert server.memory_total_bytes is None
        assert server.memory_modules == (
            {
                "slot": "1",
                "size_bytes": None,
                "type": "DRAM",
                "speed_mhz": None,
                "serial": None,
                "health": "HEALTHY",
            },
        )

    def test_storage_unread(self) -> None:
        server = _map(Vm(name="v"), storage=None, drives=None)

        assert server.storage_drives is None
        assert server.storage_total_bytes is None
        assert server.storage_controllers is None

    def test_a_drive_unread_leaves_storage_unread(self) -> None:
        server = _map(Vm(name="v"), drives=None)

        assert server.storage_drives is None
        assert server.storage_total_bytes is None
        assert server.storage_controllers is None

    def test_interfaces_unread(self) -> None:
        server = _map(Vm(name="v"), interfaces=None)

        assert server.nic_macs is None
        assert server.nics == ()


def test_controllers_for_an_empty_storage_collection_is_read_and_empty() -> None:
    assert controllers_for([], []) == ()
    assert controllers_for(None, []) is None
    assert controllers_for([], None) is None


def test_the_name_falls_back_to_the_id() -> None:
    tree = kubevirt_tree(Vm(name="v"))
    system = {k: v for k, v in tree["/redfish/v1/Systems/vms.v"].items() if k != "Name"}
    server = system_to_provider_server(
        system,
        netloc=_NETLOC,
        manager_id="m",
        processors=None,
        memory=None,
        storage=None,
        drives=None,
        interfaces=None,
    )
    assert server.name == "vms.v"
