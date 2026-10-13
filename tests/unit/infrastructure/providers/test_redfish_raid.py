"""RAID detection across the BMCs in the operator's fleet (ADR-0048).

Each layout mirrors a real BMC family: Dell iDRAC9, HPE iLO 5 (SmartStorage only) and iLO 6,
NVIDIA DGX on AMI MegaRAC (software RAID, invisible), an old Supermicro-based BMC with no
storage schema. The rule under test: PRESENT on any mirror, ABSENT only where the vendor tree
is authoritative and fully read, UNKNOWN where the BMC cannot say, None where a read failed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.redfish_fixture import RedfishFixture, minimal_service
from tests.unit.infrastructure.providers.test_redfish_collector import _collect, _provider

from app.infrastructure.providers.redfish import raid
from app.infrastructure.providers.redfish.client import RedfishClient

pytestmark = pytest.mark.unit

_DMTF_MIRROR = {"RAIDType": "RAID1", "VolumeType": "Mirrored"}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(self: object, attempt: int, retry_after: str | None) -> None:
        return None

    monkeypatch.setattr(RedfishClient, "_backoff", instant)


def _rekey(resources: dict[str, Any], system_id: str) -> dict[str, Any]:
    """Move the `Systems/1` tree to `Systems/<system_id>`, as a DGX (`Self`, `DGX`) names it."""
    text = json.dumps(resources).replace(
        "/redfish/v1/Systems/1", f"/redfish/v1/Systems/{system_id}"
    )
    moved: dict[str, Any] = json.loads(text)
    moved[f"/redfish/v1/Systems/{system_id}"]["Id"] = system_id
    return moved


def _service(
    *,
    manufacturer: str = "Dell Inc.",
    system_id: str = "1",
    members: dict[str, dict[str, Any]] | None = None,
    oem: dict[str, Any] | None = None,
    root_oem: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    A service whose `Storage` has the given members.

    `members` maps a member Id to `{"name", "volumes": list[dict] | None, "extra": dict}`;
    `volumes=None` leaves the member without a `Volumes` link.
    """
    resources = minimal_service()
    storage = "/redfish/v1/Systems/1/Storage"
    system = dict(resources["/redfish/v1/Systems/1"])
    system["Manufacturer"] = manufacturer
    if oem is not None:
        system["Oem"] = oem
    resources["/redfish/v1/Systems/1"] = system
    if root_oem is not None:
        resources["/redfish/v1/"] = {**resources["/redfish/v1/"], "Oem": root_oem}
    for key in [k for k in resources if k.startswith(storage) or "/Drives/" in k]:
        del resources[key]
    resources[storage] = {
        "@odata.id": storage,
        "Members": [{"@odata.id": f"{storage}/{i}"} for i in members or {}],
        "Members@odata.count": len(members or {}),
    }
    for member_id, spec in (members or {}).items():
        path = f"{storage}/{member_id}"
        body: dict[str, Any] = {
            "@odata.id": path,
            "@odata.type": "#Storage.v1_15_0.Storage",
            "Id": member_id,
            "Name": spec.get("name", member_id),
            "Drives": [],
            **spec.get("extra", {}),
        }
        volumes = spec.get("volumes")
        if volumes is not None:
            body["Volumes"] = {"@odata.id": f"{path}/Volumes"}
            links = []
            for n, vol in enumerate(volumes):
                vpath = f"{path}/Volumes/{n}"
                resources[vpath] = {"@odata.id": vpath, "Id": str(n), **vol}
                links.append({"@odata.id": vpath})
            resources[f"{path}/Volumes"] = {
                "@odata.id": f"{path}/Volumes",
                "Members": links,
                "Members@odata.count": len(links),
            }
        resources[path] = body
    return resources


def _smart_storage(resources: dict[str, Any], controllers: dict[str, list[str]]) -> None:
    """Add HPE's OEM tree: `controllers` maps an array controller Id to its `Raid` strings."""
    base = "/redfish/v1/Systems/1/SmartStorage"
    resources[base] = {
        "@odata.id": base,
        "Links": {"ArrayControllers": {"@odata.id": f"{base}/ArrayControllers"}},
    }
    resources[f"{base}/ArrayControllers"] = {
        "@odata.id": f"{base}/ArrayControllers",
        "Members": [{"@odata.id": f"{base}/ArrayControllers/{c}"} for c in controllers],
    }
    for controller, levels in controllers.items():
        path = f"{base}/ArrayControllers/{controller}"
        resources[path] = {
            "@odata.id": path,
            "Id": controller,
            "Model": "Smart Array P408i-p SR Gen10",
            "Links": {"LogicalDrives": {"@odata.id": f"{path}/LogicalDrives"}},
        }
        drives = [{"@odata.id": f"{path}/LogicalDrives/{n}"} for n, _ in enumerate(levels, 1)]
        resources[f"{path}/LogicalDrives"] = {
            "@odata.id": f"{path}/LogicalDrives",
            "Members": drives,
        }
        for n, level in enumerate(levels, 1):
            lpath = f"{path}/LogicalDrives/{n}"
            resources[lpath] = {"@odata.id": lpath, "Id": str(n), "Raid": level}


_HPE_OEM = {"Hpe": {"Links": {"SmartStorage": {"@odata.id": "/redfish/v1/Systems/1/SmartStorage"}}}}
_ILO5_ROOT = {"Hpe": {"Manager": [{"ManagerType": "iLO 5"}]}}


async def _check(resources: dict[str, Any], **kwargs: Any) -> tuple[Any, Any]:
    with RedfishFixture(resources=resources, **kwargs) as fixture:
        (server,) = await _collect(_provider(fixture.port))
    return server.raid_check, server


class TestDell:
    async def test_a_boss_n1_mirror_is_present(self) -> None:
        members = {"BOSS.SL.12-1": {"volumes": [_DMTF_MIRROR]}, "CPU.1": {"volumes": []}}
        check, _ = await _check(_service(members=members))
        assert check["state"] == "PRESENT"
        assert check["method"] == "redfish_storage"

    async def test_a_boss_s2_under_an_ahci_id_is_still_a_boss_card(self) -> None:
        members = {"AHCI.SL.6-1": {"name": "BOSS-S2", "volumes": [{"VolumeType": "Mirrored"}]}}
        check, server = await _check(_service(members=members))
        assert check["state"] == "PRESENT"
        assert server.storage_controllers[0]["kind"] == "BOSS"

    async def test_a_boss_s2_without_its_mirror_is_absent(self) -> None:
        members = {"AHCI.SL.6-1": {"name": "BOSS-S2", "volumes": []}}
        check, _ = await _check(_service(members=members))
        assert check["state"] == "ABSENT"

    async def test_an_nvme_only_server_is_absent(self) -> None:
        check, _ = await _check(_service(members={"CPU.1": {"volumes": []}}))
        assert check["state"] == "ABSENT"

    async def test_an_hba_only_server_is_absent_and_classed_hba(self) -> None:
        members = {"NonRAID.Slot.1-1": {"volumes": []}}
        check, server = await _check(_service(members=members))
        assert check["state"] == "ABSENT"
        assert server.storage_controllers[0]["kind"] == "HBA"

    async def test_a_raw_device_volume_is_not_a_mirror(self) -> None:
        raw = {"RAIDType": None, "VolumeType": "RawDevice"}
        check, _ = await _check(_service(members={"BOSS.SL.12-1": {"volumes": [raw]}}))
        assert check["state"] == "ABSENT"

    async def test_two_percs_with_the_mirror_on_the_second(self) -> None:
        members = {
            "RAID.Slot.1-1": {"volumes": [{"RAIDType": "RAID5"}]},
            "RAID.Slot.2-1": {"volumes": [_DMTF_MIRROR]},
        }
        check, _ = await _check(_service(members=members))
        assert check["state"] == "PRESENT"

    async def test_a_member_with_no_volumes_link_has_no_volumes(self) -> None:
        check, _ = await _check(_service(members={"AHCI.Embedded.1-1": {"volumes": None}}))
        assert check["state"] == "ABSENT"

    async def test_an_empty_storage_collection_is_unknown_not_absent(self) -> None:
        check, _ = await _check(_service(members={}))
        assert check["state"] == "UNKNOWN"

    async def test_storage_not_implemented_is_unknown(self) -> None:
        faults = {"/redfish/v1/Systems/1/Storage": 503}
        check, _ = await _check(_service(members={"BOSS.SL.12-1": {"volumes": []}}), faults=faults)
        assert check["state"] == "UNKNOWN"

    async def test_a_storage_server_error_is_unread_not_unknown(self) -> None:
        faults = {"/redfish/v1/Systems/1/Storage": 500}
        check, _ = await _check(_service(members={"BOSS.SL.12-1": {"volumes": []}}), faults=faults)
        assert check is None

    async def test_a_failed_volumes_read_is_unread_never_absent(self) -> None:
        faults = {"/redfish/v1/Systems/1/Storage/BOSS.SL.12-1/Volumes": 500}
        members = {"BOSS.SL.12-1": {"volumes": []}}
        check, server = await _check(_service(members=members), faults=faults)
        assert check is None
        assert server.storage_controllers is None

    async def test_a_mirror_still_counts_when_another_controller_failed_to_read(self) -> None:
        faults = {"/redfish/v1/Systems/1/Storage/RAID.Slot.2-1/Volumes": 500}
        members = {
            "RAID.Slot.1-1": {"volumes": [_DMTF_MIRROR]},
            "RAID.Slot.2-1": {"volumes": []},
        }
        check, _ = await _check(_service(members=members), faults=faults)
        assert check["state"] == "PRESENT"


class TestNvidiaDgx:
    """A DGX's OS mirror is mdadm inside the host: the BMC cannot see it."""

    def _dgx(self, system_id: str, member: str) -> dict[str, Any]:
        controllers = [{"Name": f"nvme{i}", "Model": "Samsung PM1733"} for i in range(13)]
        vol0 = {"CapacityBytes": 127_000_000, "VolumeType": "RawDevice"}
        members = {member: {"volumes": [vol0], "extra": {"StorageControllers": controllers}}}
        resources = _service(
            manufacturer="NVIDIA", members=members, root_oem={"Ami": {"Version": "x"}}
        )
        return _rekey(resources, system_id)

    @pytest.mark.parametrize(("system_id", "member"), [("Self", "1"), ("DGX", "StorageUnit_0")])
    async def test_a100_and_h100_layouts_are_unknown_never_absent(
        self, system_id: str, member: str
    ) -> None:
        check, server = await _check(self._dgx(system_id, member))
        assert check["state"] == "UNKNOWN"
        assert check["bmc"] == "AMI MegaRAC"
        assert "software RAID" in check["reason"]
        assert server.storage_controllers is not None

    async def test_even_an_empty_volumes_list_is_not_absent_on_another_vendor(self) -> None:
        resources = _service(manufacturer="Supermicro", members={"NVMe": {"volumes": []}})
        check, _ = await _check(resources)
        assert check["state"] == "UNKNOWN"

    async def test_a_real_raid1_volume_on_another_vendor_is_still_present(self) -> None:
        resources = _service(
            manufacturer="Lenovo", members={"RAID_Slot1": {"volumes": [_DMTF_MIRROR]}}
        )
        check, _ = await _check(resources)
        assert check["state"] == "PRESENT"


class TestHpe:
    async def test_ilo5_with_an_empty_storage_tree_reads_smartstorage(self) -> None:
        resources = _service(manufacturer="HPE", members={}, oem=_HPE_OEM, root_oem=_ILO5_ROOT)
        _smart_storage(resources, {"0": ["1"]})
        check, server = await _check(resources)
        assert check["state"] == "PRESENT"
        assert check["method"] == "hpe_smartstorage"
        assert check["bmc"] == "iLO 5"
        assert server.storage_controllers[0]["volumes"][0]["raid_type"] == "RAID1"

    @pytest.mark.parametrize("level", ["1ADM", "1Triple"])
    async def test_other_mirror_levels_are_present(self, level: str) -> None:
        resources = _service(manufacturer="HPE", members={}, oem=_HPE_OEM)
        _smart_storage(resources, {"0": [level]})
        check, _ = await _check(resources)
        assert check["state"] == "PRESENT"

    @pytest.mark.parametrize("levels", [["5"], ["10"], ["0", "6"]])
    async def test_a_smart_array_without_a_mirror_is_absent(self, levels: list[str]) -> None:
        resources = _service(manufacturer="HPE", members={}, oem=_HPE_OEM)
        _smart_storage(resources, {"0": levels})
        check, _ = await _check(resources)
        assert check["state"] == "ABSENT"
        assert check["method"] == "hpe_smartstorage"

    async def test_a_smart_array_with_no_logical_drive_is_absent(self) -> None:
        resources = _service(manufacturer="HPE", members={}, oem=_HPE_OEM)
        _smart_storage(resources, {"0": []})
        check, _ = await _check(resources)
        assert check["state"] == "ABSENT"

    async def test_no_smartstorage_and_an_empty_storage_tree_is_unknown(self) -> None:
        check, _ = await _check(_service(manufacturer="HPE", members={}, oem=_HPE_OEM))
        assert check["state"] == "UNKNOWN"

    async def test_the_two_trees_disagreeing_resolves_to_present(self) -> None:
        raw = {"VolumeType": "RawDevice"}
        resources = _service(
            manufacturer="HPE", members={"DA000005": {"volumes": [raw]}}, oem=_HPE_OEM
        )
        _smart_storage(resources, {"0": ["1"]})
        check, _ = await _check(resources)
        assert check["state"] == "PRESENT"

    async def test_ilo6_storage_only_with_a_mirror(self) -> None:
        members = {"DE042000": {"name": "NS204i-u", "volumes": [_DMTF_MIRROR]}}
        check, _ = await _check(_service(manufacturer="HPE", members=members, oem={"Hpe": {}}))
        assert check["state"] == "PRESENT"
        assert check["method"] == "redfish_storage"

    async def test_ilo6_storage_only_without_a_mirror_is_absent(self) -> None:
        members = {"DE07C000": {"volumes": [{"RAIDType": "RAID5"}]}}
        check, _ = await _check(_service(manufacturer="HPE", members=members, oem={"Hpe": {}}))
        assert check["state"] == "ABSENT"
        assert check["method"] == "redfish_storage"

    async def test_a_hpe_member_without_a_volumes_link_never_yields_absent(self) -> None:
        members = {"DE07C000": {"volumes": [{"RAIDType": "RAID5"}]}, "DA000005": {"volumes": None}}
        check, _ = await _check(_service(manufacturer="HPE", members=members, oem={"Hpe": {}}))
        assert check["state"] == "UNKNOWN"

    async def test_an_old_bmc_with_no_storage_or_smartstorage_is_unknown(self) -> None:
        resources = _service(manufacturer="HPE", members={"x": {"volumes": []}}, oem=_HPE_OEM)
        faults = {"/redfish/v1/Systems/1/Storage": 503}
        check, _ = await _check(resources, faults=faults)
        assert check["state"] == "UNKNOWN"

    async def test_a_failed_logical_drive_read_is_unread_never_absent(self) -> None:
        resources = _service(manufacturer="HPE", members={}, oem=_HPE_OEM)
        _smart_storage(resources, {"0": ["5"]})
        faults = {"/redfish/v1/Systems/1/SmartStorage/ArrayControllers/0/LogicalDrives": 500}
        check, _ = await _check(resources, faults=faults)
        assert check is None

    async def test_a_forbidden_smartstorage_is_unread(self) -> None:
        resources = _service(manufacturer="HPE", members={}, oem=_HPE_OEM)
        _smart_storage(resources, {"0": ["5"]})
        faults = {"/redfish/v1/Systems/1/SmartStorage": 403}
        check, _ = await _check(resources, faults=faults)
        assert check is None


class TestAssess:
    """The pure rule, with the inputs the provider hands it."""

    @staticmethod
    def _assess(vendor: str, **overrides: Any) -> object:
        args: dict[str, Any] = {
            "storage_status": raid.OK,
            "controllers": [{"Volumes": {"@odata.id": "/v"}}],
            "volumes_by_controller": {},
            "volumes_complete": True,
            "smart_status": raid.ABSENT_TREE,
            "smart_logical_drives": [],
            "smart_controller_count": 0,
            "bmc": None,
        }
        args.update(overrides)
        verdict = raid.assess_raid(vendor, **args)
        return None if verdict is None else verdict["state"]

    @pytest.mark.parametrize("value", ["RAID1", "raid1", " Raid1 "])
    def test_raid1_in_any_case_is_a_mirror(self, value: str) -> None:
        assert raid.is_mirror_volume({"RAIDType": value})

    @pytest.mark.parametrize("value", ["RAID10", "RAID1E", "RAID1Triple", "RAID0", "RAID5", "None"])
    def test_other_raid_types_are_not_a_mirror(self, value: str) -> None:
        assert not raid.is_mirror_volume({"RAIDType": value, "VolumeType": "Mirrored"})

    def test_mirrored_counts_only_when_raidtype_is_null(self) -> None:
        assert raid.is_mirror_volume({"RAIDType": None, "VolumeType": "Mirrored"})
        assert not raid.is_mirror_volume({"RAIDType": None, "VolumeType": "RawDevice"})
        assert not raid.is_mirror_volume({})

    def test_supported_raid_types_are_never_evidence(self) -> None:
        controllers = [{"SupportedRAIDTypes": ["RAID1"], "Volumes": {"@odata.id": "/v"}}]
        assert self._assess("dell", controllers=controllers) == "ABSENT"

    @pytest.mark.parametrize("vendor", ["standalone", "cisco", ""])
    def test_an_unknown_manufacturer_is_never_absent(self, vendor: str) -> None:
        assert self._assess(vendor) == "UNKNOWN"

    @pytest.mark.parametrize(
        ("label", "root", "expected"),
        [
            ("dell", {"Oem": {"Dell": {}}}, "iDRAC"),
            ("ilo4", {"Oem": {"Hp": {"Manager": [{"ManagerType": "iLO 4"}]}}}, "iLO 4"),
            ("ilo6", {"Oem": {"Hpe": {"Manager": [{"ManagerType": "iLO 6"}]}}}, "iLO 6"),
            ("ami", {"Oem": {"Ami": {}}}, "AMI MegaRAC"),
            ("none", {}, None),
        ],
    )
    def test_the_bmc_label_is_display_only(
        self, label: str, root: dict[str, Any], expected: str | None
    ) -> None:
        assert raid.bmc_label(root, {}) == expected
