"""Cisco Intersight managed objects -> `ProviderServer`.

Pure functions over the decoded JSON the REST API returns, with no
client and no I/O, so every rule here is testable against a recorded
payload. See docs/adr/0017-intersight-collector.md and
docs/cisco-collectors.md, "Intersight managed objects".

Two rules from the port contract are load-bearing throughout:
a field this collector could not read is `None`, never `0` or `()`; and
`"PHYSICAL"` vs `"VNIC"` on an attachment is a real distinction the
platform counts on, not a label.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.domain.ports.provider import ProviderAttachment, ProviderNic, ProviderServer
from app.infrastructure.providers.ucs_common import (
    normalize_admin_state,
    normalize_oper_state,
)

# MiB, settled live 2026-09-01 — docs/cisco-collectors.md, "Units".
_BYTES_PER_MB = 1024 * 1024

MODE_UCSM = "UCSM"
MODE_IMM = "Intersight"
MODE_STANDALONE = "IntersightStandalone"

_UNSET_ADDRESSES = frozenset({"0.0.0.0", "none", "::"})  # noqa: S104 - sentinels, not a bind


def _text(value: object) -> str | None:
    """
    A non-empty trimmed string, or None.

    Args:
        value (object): Any reported value.

    Returns:
        str | None: The trimmed text, or None when absent or blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: object) -> int | None:
    """
    An integer, or None when the value is absent or not numeric.

    Intersight reports several counts and sizes as strings, so this
    accepts either form rather than assuming the JSON type.

    Args:
        value (object): Any reported value.

    Returns:
        int | None: The integer, or None.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def moref(value: object) -> str | None:
    """
    The `Moid` a relationship field points at.

    Every parent/child link is an embedded `mo.MoRef` (`null` when
    unset); this is the fleet-wide join key — ADR-0017, "The request plan".

    Args:
        value (object): A relationship field's value.

    Returns:
        str | None: The referenced `Moid`, or None when unset.
    """
    if isinstance(value, Mapping):
        return _text(value.get("Moid"))
    return None


def management_mode(summary: Mapping[str, Any]) -> str:
    """
    Which product actually manages this server.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.

    Returns:
        str: `ManagementMode`, defaulting to `IntersightStandalone` — the
            same default the API's own schema declares for the field.
    """
    return _text(summary.get("ManagementMode")) or MODE_STANDALONE


def profile_name_from_dn(dn: str | None) -> str | None:
    """
    The service profile's name, out of its distinguished name.

    A UCSM-mode server has only the `ServiceProfile` DN, whose last component
    is `ls-<name>` — docs/cisco-collectors.md, "The server's name".

    Args:
        dn (str | None): A service profile DN, e.g.
            `"org-root/org_tlv/ls-worker-01"`.

    Returns:
        str | None: `"worker-01"`, or None if the DN names no profile.
    """
    tail = (_text(dn) or "").rsplit("/", 1)[-1]
    if not tail.startswith("ls-"):
        return None
    return tail[len("ls-") :] or None


def server_name(summary: Mapping[str, Any], profile: Mapping[str, Any] | None) -> str | None:
    """
    The name an operator would recognise this server by.

    `compute.PhysicalSummary.Name` is never an operator hostname — see
    docs/cisco-collectors.md, "The server's name", and ADR-0017.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.
        profile (Mapping[str, Any] | None): Its associated
            `server.Profile`, when it has one.

    Returns:
        str | None: The server profile's name where one is assigned, then
            the name in a UCSM service-profile DN, then the operator's own
            label, then whatever the summary calls it.
    """
    if profile is not None:
        name = _text(profile.get("Name"))
        if name:
            return name
    return (
        profile_name_from_dn(_text(summary.get("ServiceProfile")))
        or _text(summary.get("UserLabel"))
        or _text(summary.get("Name"))
    )


def bmc_address(summary: Mapping[str, Any], interface: Mapping[str, Any] | None) -> str | None:
    """
    The CIMC's out-of-band address, as a BMC URI.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`, whose
            `MgmtIpAddress` needs no extra query.
        interface (Mapping[str, Any] | None): The server's
            `management.Interface`, used only when the summary has none.

    The interface is consulted **first** where one was read. It is the
    more specific source, and it is also where `bmc_mac` comes from — so
    preferring it keeps a server's reported BMC address and BMC MAC
    describing the same interface rather than potentially two. The
    summary's own field needs no extra query and is the fallback. Whether
    the two can actually disagree is unverified; see ADR-0017.

    Returns:
        str | None: `ipmi://host:623`, the form
            `app.domain.value_objects.bmc_address.parse_bmc_address`
            already recognises for Cisco, or None.
    """
    address = None
    if interface is not None:
        address = _text(interface.get("IpAddress")) or _text(interface.get("Ipv4Address"))
    if not address:
        address = _text(summary.get("MgmtIpAddress"))
    if not address or address.lower() in _UNSET_ADDRESSES:
        return None
    return f"ipmi://{address}:623"


def memory_total_bytes(summary: Mapping[str, Any]) -> int | None:
    """
    Installed memory, in bytes.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.

    Returns:
        int | None: The total, or None when unreported. The `TotalMemory`
            unit is undocumented — see ADR-0017's UNVERIFIED list, item 1.
    """
    total = _as_int(summary.get("TotalMemory"))
    return total * _BYTES_PER_MB if total else None


def drive(disk: Mapping[str, Any]) -> dict[str, object]:
    """
    One `storage.PhysicalDisk` as the platform's drive shape.

    Args:
        disk (Mapping[str, Any]): A `storage.PhysicalDisk`.

    Returns:
        dict[str, object]: Keys mirroring
            `app.domain.models.hardware.Drive`.
    """
    return {
        "id": _text(disk.get("DiskId")) or _text(disk.get("Moid")),
        "model": _text(disk.get("Model")) or _text(disk.get("Pid")),
        "serial": _text(disk.get("Serial")),
        "media_type": _text(disk.get("Type")),
        "capacity_bytes": _capacity_bytes(disk),
        "health": _drive_health(disk),
        "health_detail": _text(disk.get("Health")) or _text(disk.get("DriveState")),
    }


def _capacity_bytes(disk: Mapping[str, Any]) -> int | None:
    """
    A drive's capacity in bytes.

    `NonCoercedSizeBytes` (bytes by name) first, `Size` (MB, a string) as
    fallback — docs/cisco-collectors.md, "Units".

    Args:
        disk (Mapping[str, Any]): A `storage.PhysicalDisk`.

    Returns:
        int | None: The capacity, or None when neither field is present.
    """
    exact = _as_int(disk.get("NonCoercedSizeBytes"))
    if exact:
        return exact
    size_mb = _as_int(disk.get("Size"))
    return size_mb * _BYTES_PER_MB if size_mb else None


def _drive_health(disk: Mapping[str, Any]) -> str:
    """
    A drive's health in the platform's vocabulary.

    `Health` first, `DriveState` only when absent; the `"ok"` entry was a
    live-found gap (ADR-0017, "The same "OK" gap, one field over", 2026-09-07).

    Args:
        disk (Mapping[str, Any]): A `storage.PhysicalDisk`.

    Returns:
        str: HEALTHY, WARNING, CRITICAL or UNKNOWN.
    """
    raw = (_text(disk.get("Health")) or _text(disk.get("DriveState")) or "").lower()
    if raw in {"ok", "good", "healthy", "online", "optimal", "jbod", "unconfigured good"}:
        return "HEALTHY"
    if raw in {"warning", "degraded", "predictive-failure", "predicted-failure", "rebuilding"}:
        return "WARNING"
    if raw in {"critical", "bad", "failed", "offline", "unconfigured bad", "foreign"}:
        return "CRITICAL"
    if str(disk.get("FailurePredicted")).lower() == "true":
        return "WARNING"
    return "UNKNOWN"


def psu(unit: Mapping[str, Any]) -> dict[str, object]:
    """
    One `equipment.Psu` as the platform's PSU shape.

    Rack/standalone servers only, and `health` is UP/DOWN/DISABLED/UNKNOWN
    — see docs/cisco-collectors.md, "Power supplies (PSUs)" (Intersight).

    Args:
        unit (Mapping[str, Any]): An `equipment.Psu`.

    Returns:
        dict[str, object]: Keys mirroring `app.domain.models.hardware.Psu`.
    """
    return {
        "id": _text(unit.get("PsuId")) or _text(unit.get("Moid")),
        "model": _text(unit.get("Model")) or _text(unit.get("Pid")),
        "serial": _text(unit.get("Serial")),
        "health": normalize_oper_state(unit.get("OperState")),
        "health_detail": _text(unit.get("OperState")),
        "capacity_watts": _as_int(unit.get("PsuWattage")),
    }


def gpu(card: Mapping[str, Any]) -> dict[str, object]:
    """
    One `graphics.Card` as the platform's GPU shape.

    Every telemetry field is `None` by construction — the API has none.
    See docs/cisco-collectors.md, "GPUs — a capability ceiling, not a gap".

    Args:
        card (Mapping[str, Any]): A `graphics.Card`.

    Returns:
        dict[str, object]: Keys mirroring `app.domain.models.hardware.Gpu`.
    """
    return {
        "model": _text(card.get("Model")) or _text(card.get("Pid")),
        "vendor": _text(card.get("Vendor")),
        "serial": _text(card.get("Serial")),
        "memory_bytes": None,
        "memory_type": None,
        "ecc_mode_enabled": None,
        "correctable_error_count": None,
        "uncorrectable_error_count": None,
        "temperature_celsius": None,
        "power_watts": None,
        "health": normalize_oper_state(card.get("OperState")),
        "health_detail": _text(card.get("OperState")),
    }


def attachment(
    interface: Mapping[str, Any], *, provider_type: str, interface_kind: str
) -> ProviderAttachment:
    """
    One adapter interface as a fabric attachment.

    Args:
        interface (Mapping[str, Any]): An `adapter.ExtEthInterface` (a
            cabled uplink) or an `adapter.HostEthInterface` (an OS-facing
            vNIC carved out of one).
        provider_type (str): The collector that observed it.
        interface_kind (str): `"PHYSICAL"` or `"VNIC"`.

    Returns:
        ProviderAttachment: The attachment. `speed_mbps` is always None —
            neither interface class carries a numeric speed, and the
            switch-side ports report a free-form string this collector
            deliberately does not guess at (ADR-0017, "Decision 5").
    """
    return ProviderAttachment(
        type="FABRIC_INTERCONNECT",
        provider=provider_type,
        fabric=_text(interface.get("SwitchId")),
        fabric_name=None,
        fabric_id=None,
        fabric_model=None,
        fabric_serial=None,
        server_interface=(
            _text(interface.get("Name"))
            or _text(interface.get("ExtEthInterfaceId"))
            or _text(interface.get("HostEthInterfaceId"))
        ),
        server_port=None,
        fabric_port=_text(interface.get("PeerDn")) or _text(interface.get("PeerPortId")),
        admin_state=normalize_admin_state(interface.get("AdminState")),
        oper_state=normalize_oper_state(interface.get("OperState")),
        speed_mbps=None,
        interface_kind=interface_kind,
    )


def _cpu_model(processors: Iterable[Mapping[str, Any]] | None) -> str | None:
    """
    The processor model string for the server.

    First socket reporting a `Model`, without filtering on `Presence` —
    see docs/cisco-collectors.md, "CPU model" (Intersight).

    Args:
        processors (Iterable[Mapping[str, Any]] | None): `processor.Unit`
            MOs owned by one server, one per socket, or None when this
            run did not query them.

    Returns:
        str | None: The first populated socket's model, or None when the
            table was not queried or no socket reported one. Which field
            actually carries the human-readable name (`Model` vs.
            `Description`) is unverified — see ADR-0017.
    """
    for processor in processors or ():
        model = _text(processor.get("Model"))
        if model:
            return model
    return None


def _macs(interfaces: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """
    Every MAC these interfaces report, in order, without duplicates.

    Args:
        interfaces (Iterable[Mapping[str, Any]]): Adapter interfaces.

    Returns:
        tuple[str, ...]: Normalized MACs.
    """
    seen: dict[str, None] = {}
    for interface in interfaces:
        mac = _text(interface.get("MacAddress"))
        if mac:
            seen.setdefault(mac.lower(), None)
    return tuple(seen)


def _nics(interfaces: Iterable[Mapping[str, Any]]) -> tuple[ProviderNic, ...]:
    """
    One `ProviderNic` per real, non-duplicate MAC these interfaces report.

    Args:
        interfaces (Iterable[Mapping[str, Any]]): Adapter interfaces —
            `adapter.ExtEthInterface` or `adapter.HostEthInterface`.

    Returns:
        tuple[ProviderNic, ...]: Same order and dedup as `_macs`.
            `speed_mbps` is always `None` — neither interface class
            carries a numeric speed (ADR-0017, "Decision 5").
    """
    nics: list[ProviderNic] = []
    seen: set[str] = set()
    for interface in interfaces:
        mac = _text(interface.get("MacAddress"))
        if not mac or mac.lower() in seen:
            continue
        seen.add(mac.lower())
        nics.append(
            ProviderNic(
                name=(
                    _text(interface.get("Name"))
                    or _text(interface.get("ExtEthInterfaceId"))
                    or _text(interface.get("HostEthInterfaceId"))
                    or ""
                ),
                mac=mac,
                speed_mbps=None,
                link_state=normalize_oper_state(interface.get("OperState")),
            )
        )
    return tuple(nics)


def to_provider_server(
    summary: Mapping[str, Any],
    *,
    provider_type: str,
    manager_id: str | None,
    profile: Mapping[str, Any] | None = None,
    template: Mapping[str, Any] | None = None,
    ext_interfaces: list[Mapping[str, Any]] | None = None,
    host_interfaces: list[Mapping[str, Any]] | None = None,
    disks: list[Mapping[str, Any]] | None = None,
    cards: list[Mapping[str, Any]] | None = None,
    processors: list[Mapping[str, Any]] | None = None,
    psus: list[Mapping[str, Any]] | None = None,
    management_interface: Mapping[str, Any] | None = None,
) -> ProviderServer:
    """
    Assemble one server from its summary and everything joined to it.

    Every sub-resource argument distinguishes "not queried" (`None`) from
    "queried, none found" (`[]`); `IngestService` carries a `None` forward.

    Args:
        summary (Mapping[str, Any]): The `compute.PhysicalSummary` anchor.
        provider_type (str): The collector's `ManagerType` value.
        manager_id (str | None): The `Manager` projection's id.
        profile (Mapping[str, Any] | None): Its `server.Profile`. Used
            for the name and the template only: unlike UCS Manager's
            `lsServer`, a `server.Profile` has no `Dn` at all, so the
            only DN available is `ServiceProfile` on a UCSM-mode summary.
        template (Mapping[str, Any] | None): The profile's source
            `server.ProfileTemplate`.
        ext_interfaces (list[Mapping[str, Any]] | None):
            `adapter.ExtEthInterface` MOs, the physical uplinks.
        host_interfaces (list[Mapping[str, Any]] | None):
            `adapter.HostEthInterface` MOs, the vNICs.
        disks (list[Mapping[str, Any]] | None): `storage.PhysicalDisk` MOs.
        cards (list[Mapping[str, Any]] | None): `graphics.Card` MOs.
        processors (list[Mapping[str, Any]] | None): `processor.Unit` MOs,
            one per socket. Added after the field-test follow-up
            (`docs/notes/intersight-inventory-model.md`, "Follow-up
            2026-09-01") reversed ADR-0017's original cut — the class is
            fleet-wide listable at the same cost as `storage.Controller`/
            `graphics.Card`, mirroring `ucs_manager.mapping._cpu_model`.
        psus (list[Mapping[str, Any]] | None): `equipment.Psu` MOs. Only
            populated for a rack/standalone server — a blade's PSUs
            belong to its chassis, not to the blade, and this MO carries
            no relationship a blade could join through.
        management_interface (Mapping[str, Any] | None): The BMC's
            `management.Interface`.

    Returns:
        ProviderServer: The normalized server.
    """
    ext = list(ext_interfaces) if ext_interfaces is not None else None
    host = list(host_interfaces) if host_interfaces is not None else None

    # vNIC MACs first. `()` is claimed only when both tables were read; a
    # failed table is `None`, so stored MACs are never blanked.
    macs: tuple[str, ...] | None = None
    host_macs = _macs(host) if host is not None else ()
    ext_macs = _macs(ext) if ext is not None else ()
    if host_macs or ext_macs:
        macs = host_macs or ext_macs
    elif host is not None and ext is not None:
        macs = ()

    # Same vNIC-first rule, so `nics` and `macs` always agree in count.
    host_nics = _nics(host) if host is not None else ()
    ext_nics = _nics(ext) if ext is not None else ()
    nics = host_nics or ext_nics

    # No `SwitchId` means not cabled: skipped, as UCS Manager does.
    attachments: list[ProviderAttachment] = [
        attachment(interface, provider_type=provider_type, interface_kind="PHYSICAL")
        for interface in ext or ()
        if _text(interface.get("SwitchId"))
    ]
    attachments.extend(
        attachment(interface, provider_type=provider_type, interface_kind="VNIC")
        for interface in host or ()
    )

    drives = [drive(disk) for disk in disks] if disks is not None else None
    storage_total: int | None = None
    if drives is not None:
        # An unreadable capacity adds nothing rather than zeroing the total.
        measured = [d["capacity_bytes"] for d in drives]
        storage_total = sum(c for c in measured if isinstance(c, int)) or None

    return ProviderServer(
        external_id=external_id(summary),
        vendor="cisco",
        name=server_name(summary, profile) or "",
        model=_text(summary.get("Model")),
        serial=_text(summary.get("Serial")),
        system_uuid=_text(summary.get("Uuid")),
        nic_macs=macs,
        nics=nics,
        bmc_address_raw=bmc_address(summary, management_interface),
        bmc_mac=_text(management_interface.get("MacAddress")) if management_interface else None,
        manager_id=manager_id,
        profile_dn=_text(summary.get("ServiceProfile")),
        profile_template_name=_text(template.get("Name")) if template else None,
        profile_template_external_id=_text(template.get("Moid")) if template else None,
        cpu_sockets=_as_int(summary.get("NumCpus")),
        cpu_cores=_as_int(summary.get("NumCpuCores")),
        cpu_threads=_as_int(summary.get("NumThreads")),
        cpu_model=_cpu_model(processors),
        memory_total_bytes=memory_total_bytes(summary),
        storage_total_bytes=storage_total,
        storage_drives=tuple(drives) if drives is not None else None,
        gpus=tuple(gpu(card) for card in cards) if cards is not None else None,
        psus=tuple(psu(unit) for unit in psus) if psus is not None else None,
        attachments=tuple(attachments),
    )


def external_id(summary: Mapping[str, Any]) -> str:
    """
    A stable identity for one server.

    `Moid` rather than `Dn`, prefixed — ADR-0017, "Decision 4".

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.

    Returns:
        str: `intersight/<Moid>`, falling back to the DN if a row somehow
            carries no `Moid`.
    """
    identity = _text(summary.get("Moid")) or _text(summary.get("Dn")) or ""
    return f"intersight/{identity}"
