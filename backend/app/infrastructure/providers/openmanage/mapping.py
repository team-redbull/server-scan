"""Pure OME JSON -> identity mapping.

No I/O: `provider.py` makes every REST call and hands this module plain
dicts to convert. The OME field names here (`ProfileName`, `TargetName`,
`DeviceServiceTag`) are validated facts carried over from a production Dell
scanner — see docs/dell-collectors.md for the provenance. `TargetName` and
`DeviceName` are display names OME derives per its console-wide "Server
Device Naming" setting, not addresses — see
docs/notes/2026-09-openmanage-device-naming-and-ip-address.md and
`_network_address` below for why the real address comes from elsewhere.

This module maps **identity only**. Hardware detail comes from each
server's iDRAC over Redfish, not from OME's `InventoryDetails` — see
docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md. The CPU,
memory, storage and NIC mappers that used to live here were deleted with
that change rather than left unreachable; they rested on heuristics (disk
capacity parsed out of the model string, threads as `2 x cores`) that
existed only because OME did not report the real values, and Redfish does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from app.domain.ports.provider import ProviderNic

# Dell's NIC FQDD, `NIC.Slot.2-4-3` — docs/dell-collectors.md, "NICs".
_FQDD_RE = re.compile(r"^NIC\.[A-Za-z]+\.(\d+)-(\d+)-(\d+)$")


def _opt_str(value: object) -> str | None:
    """
    Normalize an OME string field to a non-empty `str` or `None`.

    Args:
        value (object): A raw OME field value.

    Returns:
        str | None: The stripped string, or `None` when missing or blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _network_address(device: dict[str, Any]) -> str | None:
    """
    Read a device's real network address, immune to OME's naming policy.

    See docs/notes/2026-09-openmanage-device-naming-and-ip-address.md.

    Args:
        device (dict[str, Any]): One `/DeviceService/Devices` entry.

    Returns:
        str | None: The device's real IP/hostname, or `None` when the
            device has no management entry (or is `{}`, the undeployed
            case).
    """
    management = device.get("DeviceManagement")
    if not isinstance(management, list) or not management:
        return None
    first = management[0]
    if not isinstance(first, dict):
        return None
    return _opt_str(first.get("NetworkAddress"))


def idrac_bmc_address(idrac_ip: str | None) -> str | None:
    """
    Render a server's iDRAC IP as the Dell BMC URI the platform expects.

    See docs/dell-collectors.md, "BMC address".

    Args:
        idrac_ip (str | None): The iDRAC address, preferably the device's
            `DeviceManagement[0].NetworkAddress`, falling back to the
            profile's `TargetName` / the device's `DeviceName`.

    Returns:
        str | None: An `idrac-virtualmedia://<ip>/redfish/v1/Systems/
            System.Embedded.1` URI, or `None` when no address is known.
    """
    ip = _opt_str(idrac_ip)
    if not ip:
        return None
    return f"idrac-virtualmedia://{ip}/redfish/v1/Systems/System.Embedded.1"


def dell_port_nics(nics: tuple[ProviderNic, ...]) -> tuple[ProviderNic, ...]:
    """
    Reduce iDRAC's NPAR partition-level NICs to one entry per physical port.

    First partition kept, sorted by FQDD. See docs/dell-collectors.md, "NICs".

    Args:
        nics (tuple[ProviderNic, ...]): Every interface the BMC reported,
            as `..redfish.mapping.nics_from_interfaces` built them, with
            `location` still holding the raw FQDD.

    Returns:
        tuple[ProviderNic, ...]: One entry per physical port, each named by
            its FQDD and located as `controller/port/partition` (`1/1/1`),
            sorted by that triple, plus any interface whose identifier
            could not be parsed, appended last in the order reported.
    """
    kept: list[tuple[tuple[int, int, int], ProviderNic]] = []
    unparsed: list[ProviderNic] = []
    for nic in nics:
        match = _FQDD_RE.match(nic.location or "")
        if match is None:
            unparsed.append(nic)
            continue
        controller, port, partition = match.groups()
        if int(partition) != 1:
            continue
        kept.append(
            (
                (int(controller), int(port), int(partition)),
                replace(
                    nic,
                    name=nic.location or nic.name,
                    location=f"{controller}/{port}/{partition}",
                ),
            )
        )
    kept.sort(key=lambda entry: entry[0])
    return tuple(nic for _, nic in kept) + tuple(unparsed)


@dataclass(frozen=True, slots=True)
class OmeIdentity:
    """
    What OME alone knows about one Dell server.

    The half Redfish cannot supply (ADR-0020).

    Attributes:
        name (str): The profile name, and the server's name. Site parsing
            and classification both key off it.
        idrac_ip (str | None): The BMC address to collect hardware from —
            the device's `DeviceManagement[0].NetworkAddress` when present,
            since `TargetName`/`DeviceName` can hold an OS hostname instead
            of an address whenever OME's "Server Device Naming" console
            setting is System Hostname rather than iDRAC Hostname.
        serial (str | None): The Dell service tag, from the managed device.
        model (str | None): The device model, when OME has a managed device.
        profile_template_name (str | None): The OME deployment template
            (SPT) this profile was created from.
        profile_template_external_id (str | None): That template's OME id.
        bmc_address_raw (str | None): The Metal3-shaped BMC URI.
    """

    name: str
    idrac_ip: str | None
    serial: str | None
    model: str | None
    profile_template_name: str | None
    profile_template_external_id: str | None
    bmc_address_raw: str | None


def identity_from_profile(*, profile: dict[str, Any], device: dict[str, Any]) -> OmeIdentity:
    """
    Build one server's identity from its OME profile and managed device.

    Args:
        profile (dict[str, Any]): One `/ProfileService/Profiles` entry;
            `ProfileName` is the server name, `TargetName` its display name
            (join key, not necessarily an address — see `_network_address`),
            and `TemplateName`/`TemplateId` the deployment template it came
            from.
        device (dict[str, Any]): The `/DeviceService/Devices` entry joined by
            display name, or `{}` when OME has no managed device for the
            profile. `Model` and `DeviceServiceTag` come from here.

    Returns:
        OmeIdentity: The identity half of a collected Dell server. The site
            is intentionally absent — it is parsed from the name downstream.
    """
    name = _opt_str(profile.get("ProfileName")) or ""
    idrac_ip = (
        _network_address(device)
        or _opt_str(profile.get("TargetName"))
        or _opt_str(device.get("DeviceName"))
    )
    return OmeIdentity(
        name=name,
        idrac_ip=idrac_ip,
        serial=_opt_str(device.get("DeviceServiceTag")),
        model=_opt_str(device.get("Model")),
        profile_template_name=_opt_str(profile.get("TemplateName")),
        profile_template_external_id=_opt_str(profile.get("TemplateId")),
        bmc_address_raw=idrac_bmc_address(idrac_ip),
    )
