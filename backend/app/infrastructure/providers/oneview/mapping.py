"""Pure OneView JSON -> `ProviderServer` mapping.

No I/O: `provider.py` makes every REST call and hands this module plain
dicts. Every trap this mapping exists to avoid is recorded, with its HPE
source, in docs/hpe-collectors.md — the four that cost a fleet's data if
got wrong are:

* the name comes from the **server profile**, never from
  `server-hardware.name` (a bay location) or `serverName` (an OS
  hostname);
* `processorCoreCount` is per *processor*, so whole-system cores are
  `processorCount * processorCoreCount`;
* `memoryMb` is MiB, documented by HPE with the factor spelled out;
* a subresource whose `collectionState` is anything but `Collected`
  reports `None`, never zero — an iLO-4 server answers
  `InsufficientFirmware` for every one of them, and a server reporting
  zero drives once took a machine from CRITICAL to HEALTHY.

The subresource payloads are "in JSON format based on RedFish schema"
(HPE's words), so the Redfish collector's own `health_of`,
`media_type_of` and `is_absent` are reused rather than reimplemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.domain.enums import Vendor
from app.domain.ports.provider import ProviderNic, ProviderServer
from app.domain.value_objects.mac_address import normalize_mac
from app.infrastructure.providers.redfish.mapping import (
    health_detail_of,
    health_of,
    is_absent,
    media_type_of,
    psu_health,
)

_MIB = 1024 * 1024

# `CollectedStale` can be "missing" — docs/hpe-collectors.md, "Subresources".
_USABLE_COLLECTION_STATE = "Collected"

DEVICES = "Devices"
LOCAL_STORAGE = "LocalStorage"
LOCAL_STORAGE_V2 = "LocalStorageV2"

# Not in that enum; read from `expand=all` first, per-server call second —
# docs/hpe-collectors.md, "Power supplies" and "CPU threads".
POWER_SUPPLIES = "PowerSupplies"
PROCESSORS = "Processors"

# `Psu.health` vocabulary, never `HealthSeverity` — docs/hpe-collectors.md, "Power supplies".
_PSU_STATE_HEALTH: dict[str, str] = {
    "Ok": "UP",
    "GoodInStandby": "UP",
    "Degraded": "UNKNOWN",
    "WarningHighInputVoltage": "UNKNOWN",
    "WarningLowInputVoltage": "UNKNOWN",
    "Failed": "DOWN",
    "ACPowerLost": "DOWN",
    "OverVoltage": "DOWN",
    "OverCurrent": "DOWN",
    "OverTemperature": "DOWN",
    "FanFailure": "DOWN",
}

# `mpModel` documents one example, `iLO4` — docs/hpe-collectors.md, "iLO identity".
_ILO_GENERATION = re.compile(r"(\d+)\s*$")

# Unroutable without a zone index — docs/hpe-collectors.md, "The management-processor address".
_UNUSABLE_ADDRESS_TYPES = frozenset({"LinkLocal", "LinkLocal_Required", "SLAAC"})

# A stated assumption, best first — same section.
_ADDRESS_TYPE_ORDER = ("Static", "DHCP", "Lookup", "Undefined")


def _opt_str(value: object) -> str | None:
    """
    Normalize a OneView string field to a non-empty `str` or `None`.

    Args:
        value (object): A raw OneView field value.

    Returns:
        str | None: The stripped string, or `None` when missing or blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: object) -> int | None:
    """
    Normalize a OneView numeric field to a positive `int` or `None`.

    Zero is `None` on purpose: OneView reports `0` for a count it has not
    collected, and an unread number is `None` here, never zero.

    Args:
        value (object): A raw OneView field value.

    Returns:
        int | None: The integer when it is present and positive, else
            `None`.
    """
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def ilo_generation(mp_model: object) -> int | None:
    """
    Read the iLO generation out of `mpModel`.

    Args:
        mp_model (object): The appliance's `mpModel`, e.g. `"iLO4"`.

    Returns:
        int | None: The trailing integer, or `None` when the value is
            absent or carries no number — which is reported as unknown
            rather than assumed to be an old generation.
    """
    text = _opt_str(mp_model)
    if not text:
        return None
    match = _ILO_GENERATION.search(text)
    return int(match.group(1)) if match else None


def subresource(hardware: dict[str, Any], name: str) -> dict[str, Any]:
    """
    Find one named subresource envelope on a server-hardware member.

    Both the object-keyed and the array shape are accepted (docs/hpe-collectors.md,
    "Subresources and `collectionState`").

    Args:
        hardware (dict[str, Any]): One `/rest/server-hardware` member.
        name (str): A `SubResourceName` value, e.g. `"Devices"`.

    Returns:
        dict[str, Any]: The envelope, or `{}` when the server reports
            none by that name.
    """
    holder = hardware.get("subResources")
    if isinstance(holder, dict):
        found = holder.get(name)
        return found if isinstance(found, dict) else {}
    if isinstance(holder, list):
        for entry in holder:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
    return {}


def subresource_data(hardware: dict[str, Any], name: str) -> list[dict[str, Any]] | None:
    """
    The rows of one subresource, or `None` when it could not be read.

    Every non-`Collected` state and the unexpanded case are `None`
    (docs/hpe-collectors.md, "Subresources and `collectionState`").

    Args:
        hardware (dict[str, Any]): One `/rest/server-hardware` member.
        name (str): A `SubResourceName` value.

    Returns:
        list[dict[str, Any]] | None: The rows, `[]` for a subresource
            that was collected and is genuinely empty, or `None` when it
            could not be read.
    """
    envelope = subresource(hardware, name)
    if not envelope:
        return None
    if envelope.get("collectionState") != _USABLE_COLLECTION_STATE:
        return None
    data = envelope.get("data")
    if isinstance(data, dict):
        # Presence, not truthiness: `[]` is collected-and-empty — ADR-0022's
        # "Results, 2026-09-07".
        for key in ("Members", "Drives", "PhysicalDrives"):
            if key in data:
                data = data[key]
                break
        else:
            data = None
    if not isinstance(data, list):
        return None
    return [row for row in data if isinstance(row, dict)]


def management_processor_address(hardware: dict[str, Any]) -> str | None:
    """
    Pick the one management-processor address this server is reached at.

    `Static`, `DHCP`, `Lookup` order over `mpIpAddresses`, link-local
    discarded, `mpHostName` last (docs/hpe-collectors.md, "The management-processor address").

    Args:
        hardware (dict[str, Any]): One `/rest/server-hardware` member.

    Returns:
        str | None: The address or hostname, or `None` when the server
            reports neither.
    """
    info = hardware.get("mpHostInfo")
    if not isinstance(info, dict):
        return None
    entries = info.get("mpIpAddresses")
    usable: list[tuple[int, str]] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            address = _opt_str(entry.get("address"))
            kind = _opt_str(entry.get("type")) or "Undefined"
            if not address or kind in _UNUSABLE_ADDRESS_TYPES:
                continue
            rank = (
                _ADDRESS_TYPE_ORDER.index(kind)
                if kind in _ADDRESS_TYPE_ORDER
                else len(_ADDRESS_TYPE_ORDER)
            )
            usable.append((rank, address))
    if usable:
        usable.sort(key=lambda pair: pair[0])
        return usable[0][1]
    return _opt_str(info.get("mpHostName"))


def _nics(hardware: dict[str, Any]) -> tuple[tuple[ProviderNic, ...], tuple[str, ...]] | None:
    """
    Read the server's physical network ports out of `portMap`.

    `physicalPorts` only, never FlexNIC `virtualPorts`; no speed or link
    state exists there (docs/hpe-collectors.md, "NICs — `portMap`").

    Args:
        hardware (dict[str, Any]): One `/rest/server-hardware` member.

    Returns:
        tuple[tuple[ProviderNic, ...], tuple[str, ...]] | None: The NICs
            and their MACs, or `None` when the server reports no
            `portMap` at all.
    """
    port_map = hardware.get("portMap")
    if not isinstance(port_map, dict):
        return None
    slots = port_map.get("deviceSlots")
    if not isinstance(slots, list):
        return None

    nics: list[ProviderNic] = []
    macs: list[str] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        device = _opt_str(slot.get("deviceName")) or f"slot {slot.get('slotNumber')}"
        ports = slot.get("physicalPorts")
        if not isinstance(ports, list):
            continue
        for port in ports:
            if not isinstance(port, dict):
                continue
            mac = normalize_mac(_opt_str(port.get("mac")))
            nics.append(
                ProviderNic(
                    name=f"{device} port {port.get('portNumber')}",
                    mac=mac,
                    speed_mbps=None,
                    link_state="UNKNOWN",
                )
            )
            if mac and mac not in macs:
                macs.append(mac)
    return tuple(nics), tuple(macs)


def _gpus(hardware: dict[str, Any]) -> tuple[dict[str, object], ...] | None:
    """
    Read the GPUs out of the `Devices` subresource.

    OneView reports no GPU memory field anywhere, so `memory_bytes` is
    always `None` and the catalog fills it in (docs/hpe-collectors.md, "GPUs").

    Args:
        hardware (dict[str, Any]): One `/rest/server-hardware` member.

    Returns:
        tuple[dict[str, object], ...] | None: One entry per installed
            GPU, or `None` when the `Devices` subresource could not be
            read — which is every iLO-4 server.
    """
    devices = subresource_data(hardware, DEVICES)
    if devices is None:
        return None
    gpus: list[dict[str, object]] = []
    for device in devices:
        if device.get("DeviceType") != "GPU" or is_absent(device):
            continue
        firmware = device.get("FirmwareVersion")
        current = firmware.get("Current") if isinstance(firmware, dict) else None
        gpus.append(
            {
                "vendor": _opt_str(device.get("Manufacturer")),
                "model": _opt_str(device.get("Name")),
                "serial": _opt_str(device.get("SerialNumber")),
                "memory_bytes": None,
                "health": health_of(device),
                "health_detail": health_detail_of(device),
                "pci_address": _opt_str(device.get("Location")),
                "firmware_version": (
                    _opt_str(current.get("VersionString")) if isinstance(current, dict) else None
                ),
            }
        )
    return tuple(gpus)


def _drive_v2(drive: dict[str, Any]) -> dict[str, object]:
    """
    Map one `LocalStorageV2` drive — stock Redfish `Storage`.

    Args:
        drive (dict[str, Any]): One `Drives[]` entry.

    Returns:
        dict[str, object]: Keys mirroring
            `app.domain.models.hardware.StorageDrive`.
    """
    return {
        "id": str(drive.get("@odata.id") or drive.get("Id") or ""),
        "model": _opt_str(drive.get("Model")),
        "serial": _opt_str(drive.get("SerialNumber")),
        "media_type": media_type_of(drive),
        "protocol": _opt_str(drive.get("Protocol")),
        "capacity_bytes": _opt_int(drive.get("CapacityBytes")),
        "health": health_of(drive),
        "health_detail": health_detail_of(drive),
    }


def _drive_v1(drive: dict[str, Any]) -> dict[str, object]:
    """
    Map one `LocalStorage` drive — HPE's own SmartStorage schema.

    Capacity from `CapacityMiB` or blocks x block size, never `CapacityGB`
    (docs/hpe-collectors.md, "Storage").

    Args:
        drive (dict[str, Any]): One `PhysicalDrives[]` entry.

    Returns:
        dict[str, object]: Keys mirroring
            `app.domain.models.hardware.StorageDrive`.
    """
    mib = _opt_int(drive.get("CapacityMiB"))
    if mib is not None:
        capacity = mib * _MIB
    else:
        blocks = _opt_int(drive.get("CapacityLogicalBlocks"))
        block_size = _opt_int(drive.get("BlockSizeBytes"))
        capacity = blocks * block_size if blocks is not None and block_size is not None else None
    return {
        "id": str(drive.get("Id") or drive.get("Location") or ""),
        "model": _opt_str(drive.get("Model")),
        "serial": _opt_str(drive.get("SerialNumber")),
        "media_type": media_type_of(
            {"MediaType": drive.get("MediaType"), "Protocol": drive.get("InterfaceType")}
        ),
        "protocol": _opt_str(drive.get("InterfaceType")),
        "capacity_bytes": capacity,
        "slot": _opt_str(drive.get("Location")),
        "health": health_of(drive),
        "health_detail": health_detail_of(drive),
    }


def _physical_drives_v1(controllers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Flatten every array controller's own `PhysicalDrives[]` into one list.

    `LocalStorage.data` is per controller, never a flat drive list —
    ADR-0022's "Results, 2026-09-07".

    Args:
        controllers (list[dict[str, Any]]): The `LocalStorage` envelope's
            raw rows — one per array controller.

    Returns:
        list[dict[str, Any]]: Every controller's `PhysicalDrives[]`
            entries, concatenated in controller order.
    """
    drives: list[dict[str, Any]] = []
    for controller in controllers:
        physical = controller.get("PhysicalDrives")
        if isinstance(physical, list):
            drives.extend(row for row in physical if isinstance(row, dict))
    return drives


def _storage(
    hardware: dict[str, Any],
) -> tuple[tuple[dict[str, object], ...] | None, int | None]:
    """
    Read the server's drives from whichever local-storage schema it answers on.

    `LocalStorageV2` first; an *empty* V2 read falls back to V1 (docs/hpe-collectors.md,
    "Storage", and ADR-0022's "Results, 2026-09-07").

    Args:
        hardware (dict[str, Any]): One `/rest/server-hardware` member.

    Returns:
        tuple[tuple[dict[str, object], ...] | None, int | None]: The
            drives and their total capacity, both `None` when neither
            subresource could be read.
    """
    v2_rows = subresource_data(hardware, LOCAL_STORAGE_V2)
    if v2_rows:
        rows, mapper = v2_rows, _drive_v2
    else:
        v1_rows = subresource_data(hardware, LOCAL_STORAGE)
        if v1_rows is not None:
            rows, mapper = _physical_drives_v1(v1_rows), _drive_v1
        elif v2_rows is not None:
            # V2 read and empty, V1 unreadable: trust V2's real answer.
            rows, mapper = v2_rows, _drive_v2
        else:
            return None, None
    drives = tuple(mapper(row) for row in rows if not is_absent(row))
    sizes = [
        drive["capacity_bytes"] for drive in drives if isinstance(drive["capacity_bytes"], int)
    ]
    return drives, sum(sizes) if sizes else None


def _redfish_status_pair(status: object) -> str:
    """
    A PSU's generic Redfish `Health`/`State` pair, combined for display.

    Matches `..redfish.mapping.psus_from_supplies`'s own `health_detail`.

    Args:
        status (object): The PSU row's own `Status` field, expected to be
            a `{"Health": ..., "State": ...}` mapping.

    Returns:
        str: `"<Health>/<State>"`, `"—"` standing in for whichever half
            is missing.
    """
    status = status if isinstance(status, dict) else {}
    return f"{status.get('Health') or '—'}/{status.get('State') or '—'}"


def psus_from(rows: list[dict[str, Any]] | None) -> tuple[dict[str, object], ...] | None:
    """
    Map one server's power supplies.

    HPE's own `PowerSupplyStatus.State` decides, `psu_health` is the
    fallback; never `HealthSeverity` (docs/hpe-collectors.md, "Power supplies").

    Args:
        rows (list[dict[str, Any]] | None): `PowerSupplies` entries, or
            `None` when they could not be read this run.

    Returns:
        tuple[dict[str, object], ...] | None: Keys mirroring
            `app.domain.models.hardware.Psu`, or `None` for unread.
    """
    if rows is None:
        return None
    psus: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if is_absent(row):
            continue
        oem = row.get("Oem")
        hpe = oem.get("Hpe") if isinstance(oem, dict) else None
        status = hpe.get("PowerSupplyStatus") if isinstance(hpe, dict) else None
        state = status.get("State") if isinstance(status, dict) else None
        psus.append(
            {
                "id": str(row.get("MemberId") or row.get("Name") or index),
                "model": _opt_str(row.get("Model")),
                "serial": _opt_str(row.get("SerialNumber")),
                "health": _PSU_STATE_HEALTH.get(str(state), psu_health(row)),
                "health_detail": _opt_str(state) or _redfish_status_pair(row.get("Status")),
                "capacity_watts": _opt_int(row.get("PowerCapacityWatts")),
            }
        )
    return tuple(psus)


def cpu_threads_from(rows: list[dict[str, Any]] | None) -> int | None:
    """
    Sum every processor's own `TotalThreads` into a whole-system total.

    `/processors` is OneView's only source for threads (docs/hpe-collectors.md, "CPU threads").

    Args:
        rows (list[dict[str, Any]] | None): `Processors` entries, or
            `None` when they could not be read this run.

    Returns:
        int | None: The whole-system thread count, or `None` when the
            rows are unread or report no numeric `TotalThreads` at all.
    """
    if rows is None:
        return None
    threads = [_opt_int(row.get("TotalThreads")) for row in rows if not is_absent(row)]
    valid = [t for t in threads if t is not None]
    return sum(valid) if valid else None


@dataclass(frozen=True, slots=True)
class OneViewProfile:
    """
    The half of a HPE server only its server profile knows.

    Attributes:
        uri (str): The profile's canonical URI — the join key against
            `server-hardware.serverProfileUri`, and what this collector
            reports as `profile_dn`.
        name (str): The operator-assigned name. Site parsing and
            classification both key off it, and it exists nowhere on the
            server hardware itself.
        template_name (str | None): The server profile template this
            profile was created from.
        template_uri (str | None): That template's URI.
    """

    uri: str
    name: str
    template_name: str | None
    template_uri: str | None


def profile_from(
    profile: dict[str, Any], *, template_names: dict[str, str] | None = None
) -> OneViewProfile | None:
    """
    Build one server profile's identity.

    Args:
        profile (dict[str, Any]): One `/rest/server-profiles` member.
        template_names (dict[str, str] | None): Template URI -> template
            name, from `/rest/server-profile-templates`.

    Returns:
        OneViewProfile | None: The profile, or `None` when it carries no
            URI or no name and so can neither be joined nor used to name
            a server.
    """
    uri = _opt_str(profile.get("uri"))
    name = _opt_str(profile.get("name"))
    if not uri or not name:
        return None
    template_uri = _opt_str(profile.get("serverProfileTemplateUri"))
    return OneViewProfile(
        uri=uri,
        name=name,
        template_name=(template_names or {}).get(template_uri or ""),
        template_uri=template_uri,
    )


def server_from(
    *,
    hardware: dict[str, Any],
    profile: OneViewProfile,
    manager_id: str | None,
    power_supplies: list[dict[str, Any]] | None = None,
    processors: list[dict[str, Any]] | None = None,
) -> ProviderServer:
    """
    Map one server-hardware member and its profile onto a `ProviderServer`.

    Args:
        hardware (dict[str, Any]): One `/rest/server-hardware` member,
            fetched with `expand=all` so its subresource data is present.
        profile (OneViewProfile): The profile assigned to it, which is
            where the name comes from.
        manager_id (str | None): The `Manager` document this run reports
            under.
        power_supplies (list[dict[str, Any]] | None): This server's
            `PowerSupplies` rows — from the expanded payload when the
            appliance includes them, otherwise from the per-server
            `/powerSupplies` call. `None` means unread.
        processors (list[dict[str, Any]] | None): This server's
            `Processors` rows — from the expanded payload when the
            appliance includes them, otherwise from the per-server
            `/processors` call. `None` means unread; the only field this
            currently feeds is `cpu_threads`.

    Returns:
        ProviderServer: The server as this collector sees it. Every
            hardware field is `None` where OneView reported nothing,
            never zero.
    """
    sockets = _opt_int(hardware.get("processorCount"))
    cores_per_socket = _opt_int(hardware.get("processorCoreCount"))
    memory_mib = _opt_int(hardware.get("memoryMb"))
    nics = _nics(hardware)
    drives, storage_total = _storage(hardware)
    address = management_processor_address(hardware)

    return ProviderServer(
        external_id=str(hardware.get("uri") or profile.uri),
        vendor=Vendor.HP.value,
        # From the profile, never `hardware["name"]` — docs/hpe-collectors.md, "The name trap".
        name=profile.name,
        model=_opt_str(hardware.get("model")),
        # The physical serial, never the profile's virtual one — "The join, and the serial".
        serial=_opt_str(hardware.get("serialNumber")),
        system_uuid=_opt_str(hardware.get("uuid")),
        nic_macs=nics[1] if nics is not None else None,
        bmc_address_raw=f"https://{address}" if address else None,
        nics=nics[0] if nics is not None else (),
        manager_id=manager_id,
        profile_dn=profile.uri,
        profile_template_name=profile.template_name,
        profile_template_external_id=profile.template_uri,
        cpu_sockets=sockets,
        # `processorCoreCount` is per processor — docs/hpe-collectors.md, "CPU, memory".
        cpu_cores=(
            sockets * cores_per_socket
            if sockets is not None and cores_per_socket is not None
            else None
        ),
        cpu_threads=cpu_threads_from(processors),
        cpu_model=_opt_str(hardware.get("processorType")),
        memory_total_bytes=memory_mib * _MIB if memory_mib is not None else None,
        storage_total_bytes=storage_total,
        storage_drives=drives,
        gpus=_gpus(hardware),
        psus=psus_from(power_supplies),
    )
