"""Decide whether a UCS server mirrors its OS disks (ADR-0048).

Pure functions over `storageController`, `storageVirtualDrive` and `storageLocalDisk` MOs, shared
by a scan and `get_one`. UCS Manager lists every virtual drive of a server, so a RAID-capable
controller with no mirror is `ABSENT`; a server with no such controller visible is `UNKNOWN`.
See docs/cisco-collectors.md, "Storage controllers and RAID".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

PRESENT = "PRESENT"
ABSENT = "ABSENT"
UNKNOWN = "UNKNOWN"

_BYTES_PER_MB = 1024 * 1024

# `storageVirtualDrive.type` (ucsmsdk enum) -> the DMTF spelling the Redfish collectors store.
_RAID_TYPES = {
    "mirror": "RAID1",
    "stripe": "RAID0",
    "stripe-parity": "RAID5",
    "stripe-dual-parity": "RAID6",
    "mirror-stripe": "RAID10",
    "stripe-parity-stripe": "RAID50",
    "stripe-dual-parity-stripe": "RAID60",
}

# `storageVirtualDrive.drive_state` -> `HealthSeverity`; `unknown`/unmapped stay unread.
_VD_HEALTH = {
    "optimal": "HEALTHY",
    "degraded": "WARNING",
    "partially-degraded": "WARNING",
    "cache-degraded": "WARNING",
    "rebuilding": "WARNING",
    "offline": "CRITICAL",
}

# Controller types that are never a RAID card on their own (chipset, flash, pass-through, HBA).
_NOT_RAID_TYPES = frozenset({"FCH", "FLASH", "HBA", "PCH", "PT"})


def raid_type(virtual_drive: Any) -> str | None:
    """
    Spell a virtual drive's `type` the DMTF way.

    Args:
        virtual_drive (Any): A `storageVirtualDrive` MO.

    Returns:
        str | None: `RAID1`, `RAID5`, ... or None for `simple`, `raid` and `unspecified`.
    """
    return _RAID_TYPES.get(str(getattr(virtual_drive, "type", "") or "").lower())


def is_raid_capable(controller: Any) -> bool:
    """
    Whether a `storageController` can host a virtual drive.

    The most conservative reading of the SDK: a chipset, flash, HBA or pass-through type never
    counts, and otherwise `raid_support` or `device_raid_support` must name a RAID level.

    Args:
        controller (Any): A `storageController` MO.

    Returns:
        bool: True when UCS Manager reports RAID support for it.
    """
    if str(getattr(controller, "type", "") or "").upper() in _NOT_RAID_TYPES:
        return False
    return any(
        "raid" in str(getattr(controller, attr, "") or "").lower()
        for attr in ("raid_support", "device_raid_support")
    )


def _controller_kind(controller: Any, *, capable: bool) -> str:
    """
    Label a controller for display.

    Args:
        controller (Any): A `storageController` MO.
        capable (bool): The result of `is_raid_capable` or "owns a virtual drive".

    Returns:
        str: `RAID`, `NVME`, `HBA` or `OTHER`.
    """
    kind = str(getattr(controller, "type", "") or "").upper()
    if capable:
        return "RAID"
    return kind if kind in ("NVME", "HBA") else "OTHER"


def _volume(virtual_drive: Any) -> dict[str, object]:
    """
    One virtual drive as a `StorageController.volumes` entry.

    `admin_state` is ignored on purpose: UCS leaves a stale `delete` on healthy drives.

    Args:
        virtual_drive (Any): A `storageVirtualDrive` MO.

    Returns:
        dict[str, object]: Keys mirror `hardware.StorageVolume`.
    """
    size_mb = str(getattr(virtual_drive, "size", "") or "")
    return {
        "id": str(getattr(virtual_drive, "dn", "") or "").rsplit("/", 1)[-1],
        "raid_type": raid_type(virtual_drive),
        "volume_type": str(getattr(virtual_drive, "type", "") or "") or None,
        "capacity_bytes": int(size_mb) * _BYTES_PER_MB if size_mb.isdigit() else None,
        "health": _VD_HEALTH.get(str(getattr(virtual_drive, "drive_state", "") or "").lower()),
    }


def _owner(dn: str, controller_dns: Iterable[str]) -> str | None:
    """
    The controller a DN hangs below (longest prefix).

    Args:
        dn (str): A virtual drive or disk DN.
        controller_dns (Iterable[str]): The server's controller DNs.

    Returns:
        str | None: The owning controller DN, or None.
    """
    owners = [c for c in controller_dns if dn.startswith(f"{c}/")]
    return max(owners, key=len) if owners else None


def assess_ucs_raid(
    controllers: list[Any], virtual_drives: list[Any], disk_units: list[Any]
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """
    Summarize one server's controllers and decide `PRESENT`, `ABSENT` or `UNKNOWN`.

    Args:
        controllers (list[Any]): The server's `storageController` MOs.
        virtual_drives (list[Any]): The server's `storageVirtualDrive` MOs.
        disk_units (list[Any]): The server's `storageLocalDisk` MOs, for drive counts.

    Returns:
        tuple[tuple[dict[str, object], ...], dict[str, object]]: The controller entries and the
            `RaidCheck` dict. A mirror anywhere is `PRESENT` (health of its legs is the storage
            category's job); `ABSENT` needs a RAID-capable controller and no mirror.
    """
    controller_dns = [str(c.dn) for c in controllers]
    vds_by_controller: dict[str, list[Any]] = {dn: [] for dn in controller_dns}
    for vd in virtual_drives:
        owner = _owner(str(getattr(vd, "dn", "") or ""), controller_dns)
        if owner is not None:
            vds_by_controller[owner].append(vd)
    entries: list[dict[str, object]] = []
    for controller in controllers:
        dn = str(controller.dn)
        owned = vds_by_controller[dn]
        capable = is_raid_capable(controller) or bool(owned)
        entries.append(
            {
                "id": dn.rsplit("/", 1)[-1],
                "name": str(getattr(controller, "model", "") or "") or None,
                "kind": _controller_kind(controller, capable=capable),
                "drive_count": sum(
                    1 for d in disk_units if str(getattr(d, "dn", "")).startswith(f"{dn}/")
                ),
                "volumes": tuple(_volume(vd) for vd in owned),
            }
        )
    capable_any = any(e["kind"] == "RAID" for e in entries)
    mirrored = any(raid_type(vd) == "RAID1" for vd in virtual_drives)
    if mirrored:
        state, reason = PRESENT, None
    elif capable_any:
        state, reason = ABSENT, "no RAID 1 virtual drive on any RAID controller"
    else:
        state, reason = UNKNOWN, "no RAID-capable storage controller visible to UCS Manager"
    return tuple(entries), {
        "state": state,
        "method": "ucs_virtual_drive",
        "bmc": "UCS Manager",
        "reason": reason,
    }
