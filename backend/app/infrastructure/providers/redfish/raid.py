"""Decide whether a Redfish server mirrors its OS disks (ADR-0048).

Pure functions, no I/O: `provider.py` reads the trees and hands plain dicts here.
The collector, not the health engine, decides `ABSENT`, because only it knows which
vendor tree is authoritative; a BMC that cannot see the answer is `UNKNOWN`.
"""

from __future__ import annotations

from typing import Any

PRESENT = "PRESENT"
ABSENT = "ABSENT"
UNKNOWN = "UNKNOWN"

# HPE OEM `LogicalDrive.Raid` read values that keep two copies (strings without the word RAID).
_HPE_MIRRORS = frozenset({"1", "1ADM", "1TRIPLE"})

# Statuses of a tree read: "ok", "absent" (no such tree on this BMC), "failed" (try again later).
OK = "ok"
ABSENT_TREE = "absent"
FAILED = "failed"


def is_mirror_volume(volume: dict[str, Any]) -> bool:
    """
    Whether a DMTF `Volume` is a two-copy mirror.

    `VolumeType: Mirrored` is trusted only when `RAIDType` says nothing (older schemas).

    Args:
        volume (dict[str, Any]): A `Volume` resource.

    Returns:
        bool: True for `RAIDType` `RAID1` (any case), or a `Mirrored` volume with no `RAIDType`.
    """
    raid_type = volume.get("RAIDType")
    if isinstance(raid_type, str) and raid_type.strip():
        return raid_type.strip().upper() == "RAID1"
    return str(volume.get("VolumeType") or "").strip().lower() == "mirrored"


def is_hpe_mirror(logical_drive: dict[str, Any]) -> bool:
    """
    Whether an HPE OEM `LogicalDrive` is a mirror.

    Args:
        logical_drive (dict[str, Any]): One `SmartStorage` logical drive.

    Returns:
        bool: True for `Raid` `1`, `1ADM` or `1Triple`.
    """
    return str(logical_drive.get("Raid") or "").strip().upper() in _HPE_MIRRORS


def hpe_raid_type(logical_drive: dict[str, Any]) -> str | None:
    """
    Spell an HPE `Raid` value the DMTF way, for display and evidence counts.

    Args:
        logical_drive (dict[str, Any]): One `SmartStorage` logical drive.

    Returns:
        str | None: `RAID1`, `RAID10`, ... or None when the drive names no level.
    """
    raw = str(logical_drive.get("Raid") or "").strip()
    return f"RAID{raw.upper()}" if raw else None


def bmc_label(service_root: dict[str, Any], system: dict[str, Any]) -> str | None:
    """
    A display label for the management controller, from the service root's own claims.

    Never used to decide anything (ADR-0048): a wrong guess here costs a label, not a verdict.

    Args:
        service_root (dict[str, Any]): `/redfish/v1`.
        system (dict[str, Any]): The `ComputerSystem`.

    Returns:
        str | None: e.g. `iDRAC`, `iLO 5`, `AMI MegaRAC`, `Supermicro BMC`, or None.
    """
    oem = service_root.get("Oem")
    oem_keys = {str(k).lower() for k in oem} if isinstance(oem, dict) else set()
    product = str(service_root.get("Product") or "")
    if "dell" in oem_keys or "idrac" in product.lower():
        return "iDRAC"
    for key in ("Hpe", "Hp"):
        block = oem.get(key) if isinstance(oem, dict) else None
        managers = block.get("Manager") if isinstance(block, dict) else None
        if isinstance(managers, list) and managers and isinstance(managers[0], dict):
            kind = str(managers[0].get("ManagerType") or "").strip()
            if kind:
                return kind
        if block is not None:
            return "iLO"
    if "ami" in oem_keys or "ami" in str(service_root.get("Vendor") or "").lower():
        return "AMI MegaRAC"
    if "supermicro" in oem_keys:
        return "Supermicro BMC"
    if "lenovo" in oem_keys:
        return "Lenovo XCC"
    if "openbmc" in oem_keys or "openbmc" in product.lower():
        return "OpenBMC"
    return product or None


def _result(
    state: str, method: str | None, bmc: str | None, reason: str | None
) -> dict[str, object]:
    return {"state": state, "method": method, "bmc": bmc, "reason": reason}


def assess_raid(
    vendor: str,
    *,
    storage_status: str,
    controllers: list[dict[str, Any]] | None,
    volumes_by_controller: dict[str, list[dict[str, Any]]],
    volumes_complete: bool,
    smart_status: str,
    smart_logical_drives: list[dict[str, Any]],
    smart_controller_count: int,
    bmc: str | None,
) -> dict[str, object] | None:
    """
    Decide `PRESENT`, `ABSENT` or `UNKNOWN`, or None when a source could not be read.

    Any mirror wins; `ABSENT` only from a complete read of a vendor tree that is authoritative.
    See docs/adr/0048 for the per-vendor rules.

    Args:
        vendor (str): The server's `Vendor` value (`dell`, `hp`, `standalone`, ...).
        storage_status (str): Read status of `Systems/<id>/Storage` (`ok`/`absent`/`failed`).
        controllers (list[dict[str, Any]] | None): The `Storage` members, when read.
        volumes_by_controller (dict[str, list[dict[str, Any]]]): Volumes read, by member id.
        volumes_complete (bool): False when any `Volumes` collection could not be read.
        smart_status (str): Read status of HPE's `SmartStorage` tree (`absent` when not tried).
        smart_logical_drives (list[dict[str, Any]]): Every `LogicalDrive` read from it.
        smart_controller_count (int): How many `ArrayControllers` it listed.
        bmc (str | None): The display label from `bmc_label`.

    Returns:
        dict[str, object] | None: `{state, method, bmc, reason}`, or None when no mirror was
            seen and a source failed to read (the stored verdict is kept; never "no RAID").
    """
    members = controllers or []
    volumes = [v for vols in volumes_by_controller.values() for v in vols]
    if any(is_mirror_volume(v) for v in volumes):
        return _result(PRESENT, "redfish_storage", bmc, None)
    if any(is_hpe_mirror(ld) for ld in smart_logical_drives):
        return _result(PRESENT, "hpe_smartstorage", bmc, None)

    if FAILED in (storage_status, smart_status) or (storage_status == OK and not volumes_complete):
        return None

    if vendor == "dell" and storage_status == OK and members:
        return _result(ABSENT, "redfish_storage", bmc, "no RAID 1 virtual disk on any controller")
    if vendor == "hp":
        if smart_status == OK and smart_controller_count > 0:
            return _result(
                ABSENT, "hpe_smartstorage", bmc, "no RAID 1 logical drive on any array controller"
            )
        all_have_volumes = all(isinstance(m.get("Volumes"), dict) for m in members)
        if storage_status == OK and members and all_have_volumes:
            return _result(ABSENT, "redfish_storage", bmc, "no RAID 1 volume on any controller")
        return _result(
            UNKNOWN,
            None,
            bmc,
            "neither the Storage nor the SmartStorage tree lists a RAID controller",
        )
    if vendor == "dell":
        return _result(UNKNOWN, None, bmc, "the BMC lists no storage controllers")
    return _result(
        UNKNOWN,
        None,
        bmc,
        "software RAID, or a controller the BMC cannot see, is invisible over Redfish",
    )
