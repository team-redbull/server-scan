"""UCS RAID 1 detection (ADR-0048), against shapes the operator's live domain returned."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.unit.infrastructure.providers.test_ucs_manager_provider import (
    FakeUcsClient,
    FakeUcsClientWithDn,
    _collect,
    _domain,
    _provider,
)

from app.domain.ports.provider import ServerIdentity
from app.infrastructure.providers.ucs_manager.raid import (
    assess_ucs_raid,
    is_raid_capable,
    raid_type,
)

pytestmark = pytest.mark.unit

_RACK = "sys/rack-unit-3"
_BLADE = "sys/chassis-1/blade-1"


def _controller(unit: str = _RACK, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "dn": f"{unit}/board/storage-SATA-3",
        "type": "SATA",
        "model": "Cisco 12G SAS RAID",
        "raid_support": "RAID0,RAID1,RAID10",
        "device_raid_support": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _vd(unit: str = _RACK, **overrides: Any) -> SimpleNamespace:
    """A virtual drive as the live domain returned it, stale `adminState: delete` included."""
    base: dict[str, Any] = {
        "dn": f"{unit}/board/storage-SATA-3/vd-1000",
        "type": "mirror",
        "size": "457798",
        "drive_state": "optimal",
        "bootable": "true",
        "admin_state": "delete",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _disk(unit: str, n: int, state: str) -> SimpleNamespace:
    return SimpleNamespace(
        dn=f"{unit}/board/storage-SATA-3/disk-{n}",
        presence="equipped",
        model="Micron 5300",
        serial=f"S{n}",
        device_type="SSD",
        disk_state=state,
        size="457862",
    )


class TestAssess:
    def test_a_mirror_is_present_whatever_the_stale_admin_state_says(self) -> None:
        _, check = assess_ucs_raid([_controller()], [_vd()], [])
        assert check == {
            "state": "PRESENT",
            "method": "ucs_virtual_drive",
            "bmc": "UCS Manager",
            "reason": None,
        }

    def test_a_degraded_or_offline_mirror_is_still_configured(self) -> None:
        for state in ("degraded", "offline", "rebuilding"):
            _, check = assess_ucs_raid([_controller()], [_vd(drive_state=state)], [])
            assert check["state"] == "PRESENT"

    def test_a_raid_controller_with_no_virtual_drive_is_absent(self) -> None:
        _, check = assess_ucs_raid([_controller()], [], [])
        assert check["state"] == "ABSENT"

    def test_a_stripe_or_raid5_is_not_an_os_mirror(self) -> None:
        for kind in ("stripe", "stripe-parity", "mirror-stripe", "simple"):
            _, check = assess_ucs_raid([_controller()], [_vd(type=kind)], [])
            assert check["state"] == "ABSENT"

    @pytest.mark.parametrize("kind", ["PCH", "FCH", "FLASH", "HBA", "PT"])
    def test_a_chipset_flash_or_hba_controller_never_gives_absent(self, kind: str) -> None:
        _, check = assess_ucs_raid([_controller(type=kind)], [], [])
        assert check["state"] == "UNKNOWN"

    def test_a_controller_naming_no_raid_level_is_unknown(self) -> None:
        controller = _controller(raid_support="", device_raid_support="")
        assert not is_raid_capable(controller)
        _, check = assess_ucs_raid([controller], [], [])
        assert check["state"] == "UNKNOWN"

    def test_no_controller_at_all_is_unknown(self) -> None:
        _, check = assess_ucs_raid([], [], [])
        assert check["state"] == "UNKNOWN"
        assert check["reason"]

    def test_a_virtual_drive_makes_its_controller_raid_capable(self) -> None:
        controller = _controller(raid_support="", type="M2")
        entries, check = assess_ucs_raid([controller], [_vd(type="stripe")], [])
        assert entries[0]["kind"] == "RAID"
        assert check["state"] == "ABSENT"

    def test_the_controller_entry_carries_volumes_and_drive_counts(self) -> None:
        disks = [_disk(_RACK, 1, "online"), _disk(_RACK, 2, "online")]
        entries, _ = assess_ucs_raid([_controller()], [_vd()], disks)
        (entry,) = entries
        assert entry["id"] == "storage-SATA-3"
        assert entry["drive_count"] == 2
        volumes = cast(Any, entry["volumes"])
        (volume,) = volumes
        assert volume["raid_type"] == "RAID1"
        assert volume["health"] == "HEALTHY"
        assert volume["capacity_bytes"] == 457798 * 1024 * 1024

    def test_virtual_drives_attach_to_the_right_controller(self) -> None:
        other = _controller(dn=f"{_RACK}/board/storage-SAS-1", raid_support="RAID0")
        stripe = _vd(dn=f"{_RACK}/board/storage-SAS-1/vd-1", type="stripe")
        entries, check = assess_ucs_raid([_controller(), other], [_vd(), stripe], [])
        assert [len(cast(Any, e["volumes"])) for e in entries] == [1, 1]
        assert check["state"] == "PRESENT"

    @pytest.mark.parametrize(
        ("wire", "expected"),
        [("mirror", "RAID1"), ("stripe-parity", "RAID5"), ("mirror-stripe", "RAID10")],
    )
    def test_the_type_enum_maps_to_the_dmtf_spelling(self, wire: str, expected: str) -> None:
        assert raid_type(_vd(type=wire)) == expected

    def test_an_unspecified_type_has_no_level(self) -> None:
        assert raid_type(_vd(type="unspecified")) is None


class TestOnBothServerKinds:
    async def _scan(self, unit: str) -> Any:
        domain = _domain()
        if unit == _RACK:
            domain["computeBlade"], domain["computeRackUnit"] = (
                [],
                [SimpleNamespace(**{**vars(_domain()["computeBlade"][0]), "dn": _RACK})],
            )
        domain["storageController"] = [_controller(unit)]
        domain["storageVirtualDrive"] = [_vd(unit)]
        domain["storageLocalDisk"] = [_disk(unit, 1, "online"), _disk(unit, 2, "unconfigured-good")]
        [server] = await _collect(_provider(FakeUcsClient(responses=domain)))
        return server

    @pytest.mark.parametrize("unit", [_RACK, _BLADE])
    async def test_rack_units_and_blades_both_get_a_verdict(self, unit: str) -> None:
        server = await self._scan(unit)
        assert server.raid_check is not None
        assert server.raid_check["state"] == "PRESENT"
        assert server.storage_controllers is not None
        assert server.storage_controllers[0]["drive_count"] == 2

    async def test_an_unconfigured_good_disk_is_healthy_not_failed(self) -> None:
        server = await self._scan(_RACK)
        assert server.storage_drives is not None
        assert {d["health"] for d in server.storage_drives} == {"HEALTHY"}

    async def test_no_extra_query_per_server(self) -> None:
        domain = _domain()
        client = FakeUcsClient(responses=domain)
        await _collect(_provider(client))
        assert client.calls.count("query_classid:storageVirtualDrive") == 1
        assert client.calls.count("query_classid:storageController") == 1

    async def test_a_domain_whose_controllers_have_no_virtual_drive_is_absent(self) -> None:
        domain = _domain()
        domain["storageController"] = [_controller(_BLADE)]
        [server] = await _collect(_provider(FakeUcsClient(responses=domain)))
        assert server.raid_check is not None
        assert server.raid_check["state"] == "ABSENT"

    async def test_a_domain_with_no_controller_is_unknown_not_absent(self) -> None:
        [server] = await _collect(_provider(FakeUcsClient(responses=_domain())))
        assert server.raid_check is not None
        assert server.raid_check["state"] == "UNKNOWN"


class TestGetOneEqualsScan:
    @pytest.mark.parametrize("with_vd", [True, False])
    async def test_the_recheck_reads_the_same_storage_a_scan_does(self, with_vd: bool) -> None:
        domain = _domain()
        domain["storageController"] = [_controller(_BLADE)]
        domain["storageVirtualDrive"] = [_vd(_BLADE)] if with_vd else []
        [scanned] = await _collect(_provider(FakeUcsClient(responses=domain)))

        client = FakeUcsClientWithDn(responses=domain, server_dn=_BLADE)
        fetched = await _provider(client).get_one(ServerIdentity(external_id=_BLADE))

        assert fetched is not None
        assert fetched.raid_check == scanned.raid_check
        assert fetched.storage_controllers == scanned.storage_controllers
        assert fetched.raid_check is not None
        assert fetched.raid_check["state"] == ("PRESENT" if with_vd else "ABSENT")
