"""Pure `ucsmsdk` managed-object -> `ProviderServer` mapping.

No I/O: `provider.py` makes every XML API call and hands this module plain
MOs to convert. Shared unchanged with the UCS Central collector.

See docs/cisco-collectors.md, "CPU, memory and storage" for the unit
assumptions this module rests on.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from app.domain.ports.provider import ProviderAttachment, ProviderNic, ProviderServer
from app.infrastructure.providers.ucs_common import (
    is_equipped,
    normalize_admin_state,
    normalize_oper_state,
)

_BYTES_PER_MB = 1024 * 1024
_NOT_APPLICABLE = "not-applicable"


def _profile_template_fields(
    profile: Any | None,
    *,
    template_dn_by_name: dict[str, str],
) -> tuple[str | None, str | None]:
    """
    Resolve the service-profile template a profile was derived from.

    Args:
        profile (Any | None): The server's `lsServer` service profile, or
            `None` if it has none.
        template_dn_by_name (dict[str, str]): Template name -> DN. Lossy
            across orgs by construction, so it is only a fallback.

    Returns:
        tuple[str | None, str | None]: `(name, external_id)` for
            `ProviderServer.profile_template_*`, or `(None, None)` when the
            server has no profile or its profile came from no template.

    See docs/cisco-collectors.md, "Service profiles and server names".
    """
    if profile is None:
        return None, None
    template_name = getattr(profile, "src_templ_name", None) or None
    if not template_name:
        return None, None
    oper_dn = getattr(profile, "oper_src_templ_name", None) or None
    external_id = oper_dn or template_dn_by_name.get(template_name, template_name)
    return template_name, external_id


def _server_name(server_mo: Any, profile: Any | None) -> str:
    """
    The name an operator would use for this machine.

    Args:
        server_mo (Any): The `computeBlade` or `computeRackUnit` MO.
        profile (Any | None): Its associated `lsServer`, or `None`.

    Returns:
        str: The service profile's name, else the MO's own name, else its
            DN as a last resort.

    See docs/cisco-collectors.md, "Service profiles and server names".
    """
    profile_name = getattr(profile, "name", None) if profile is not None else None
    return profile_name or getattr(server_mo, "name", None) or str(server_mo.dn)


def _management_ip_addr(
    *, profile: Any | None, server_mo: Any, mgmt_ip_by_parent_dn: dict[str, Any]
) -> Any | None:
    """
    Resolve a server's management IP MO, trying both DNs it can legitimately hang off of.

    Profile DN first (the one populated on real hardware), then the compute
    unit's `mgmtController` DN — docs/cisco-collectors.md, "BMC and management".

    Args:
        profile (Any | None): The server's `lsServer` service profile, or
            `None`.
        server_mo (Any): The `computeBlade` or `computeRackUnit` MO.
        mgmt_ip_by_parent_dn (dict[str, Any]): From
            `ucs_common.management_ip_by_parent_dn`.

    Returns:
        Any | None: The resolved MO, or `None` if neither DN has one.
    """
    if profile is not None:
        by_profile = mgmt_ip_by_parent_dn.get(profile.dn)
        if by_profile is not None:
            return by_profile
    return mgmt_ip_by_parent_dn.get(f"{server_mo.dn}/mgmt")


def _bmc_address(mgmt_if: Any | None, mgmt_ip_addr: Any | None) -> str | None:
    """
    The CIMC's out-of-band address as a BMC URI.

    Args:
        mgmt_if (Any | None): The server's own `mgmtIf`, or `None`.
        mgmt_ip_addr (Any | None): Its resolved
            `vnicIpV4PooledAddr`/`vnicIpV4StaticAddr`, from
            `_management_ip_addr`, or `None`.

    Returns:
        str | None: An `ipmi://host:623` URI in the form
            `app.domain.value_objects.bmc_address.parse_bmc_address`
            recognizes for Cisco, or `None` when neither source has an
            address.

    See docs/cisco-collectors.md, "BMC and management interface selection".
    """
    addr = getattr(mgmt_ip_addr, "addr", None) if mgmt_ip_addr is not None else None
    if not addr and mgmt_if is not None:
        addr = getattr(mgmt_if, "ext_ip", None)
    if not addr or addr in ("0.0.0.0", "none"):  # noqa: S104 - unset-IP sentinels, not a bind
        return None
    return f"ipmi://{addr}:623"


def _extract_macs(adapter_ifs: list[Any]) -> tuple[str, ...]:
    """
    Pull real MACs off a list of adapter interfaces.

    Args:
        adapter_ifs (list[Any]): `adaptorHostEthIf` or `adaptorExtEthIf`
            MOs.

    Returns:
        tuple[str, ...]: Their MACs in order, skipping UCS's
            "not applicable" and "derived" placeholders.
    """
    macs: list[str] = []
    for mo in adapter_ifs:
        mac = getattr(mo, "mac", None)
        if mac and mac.lower() not in ("not applicable", "derived"):
            macs.append(mac)
    return tuple(macs)


def _nic_macs(*, host_eth_ifs: list[Any], ext_eth_ifs: list[Any]) -> tuple[str, ...]:
    """
    The MACs an OS on this server would report on its own NICs.

    Args:
        host_eth_ifs (list[Any]): `adaptorHostEthIf` (logical vNIC) MOs.
        ext_eth_ifs (list[Any]): `adaptorExtEthIf` (physical port) MOs.

    Returns:
        tuple[str, ...]: vNIC MACs, falling back to physical-port MACs only
            when the server has no vNIC at all.

    See docs/cisco-collectors.md, "Adapter interfaces, MACs and fabric
    attachments".
    """
    host_macs = _extract_macs(host_eth_ifs)
    return host_macs if host_macs else _extract_macs(ext_eth_ifs)


def _extract_nics(
    adapter_ifs: list[Any], *, link_state_of: Callable[[Any], str]
) -> tuple[ProviderNic, ...]:
    """
    Build one `ProviderNic` per real MAC off a list of adapter interfaces.

    Args:
        adapter_ifs (list[Any]): `adaptorHostEthIf` or `adaptorExtEthIf` MOs.
        link_state_of (Callable[[Any], str]): `_oper_state` for physical
            ports, `_vnic_link_state` for vNICs — never both in the same
            call, matching `adapter_ifs`'s own contract.

    Returns:
        tuple[ProviderNic, ...]: Same filtering as `_extract_macs`, in the
            same order — `speed_mbps` is always `None`, UCS Manager
            reports no numeric interface speed.
    """
    nics: list[ProviderNic] = []
    for mo in adapter_ifs:
        mac = getattr(mo, "mac", None)
        if not mac or mac.lower() in ("not applicable", "derived"):
            continue
        nics.append(
            ProviderNic(
                name=getattr(mo, "name", None) or getattr(mo, "id", None) or "",
                mac=mac,
                speed_mbps=None,
                link_state=link_state_of(mo),
            )
        )
    return tuple(nics)


def _nics(*, host_eth_ifs: list[Any], ext_eth_ifs: list[Any]) -> tuple[ProviderNic, ...]:
    """
    The interfaces themselves, mirroring `_nic_macs`'s vNIC-first rule.

    Args:
        host_eth_ifs (list[Any]): `adaptorHostEthIf` (logical vNIC) MOs.
        ext_eth_ifs (list[Any]): `adaptorExtEthIf` (physical port) MOs.

    Returns:
        tuple[ProviderNic, ...]: One entry per MAC `_nic_macs` would have
            counted, in the same order, so interface and MAC counts agree.
            A vNIC's `link_state` comes from `_vnic_link_state`
            (`operability`), a physical port's from `_oper_state`.
    """
    host_nics = _extract_nics(host_eth_ifs, link_state_of=_vnic_link_state)
    return host_nics if host_nics else _extract_nics(ext_eth_ifs, link_state_of=_oper_state)


def _oper_state(mo: Any) -> str:
    """
    Map UCS's operational-state vocabulary onto the platform's.

    Args:
        mo (Any): Any MO carrying an `oper_state` property.

    Returns:
        str: UP, DOWN, DISABLED, or UNKNOWN for an unrecognized value.
    """
    return normalize_oper_state(getattr(mo, "oper_state", None))


def _vnic_link_state(mo: Any) -> str:
    """
    A vNIC's link state from `operability`, not `oper_state`.

    See docs/cisco-collectors.md, "`operability` — a second vNIC signal
    ADR-0009 did not check".

    Args:
        mo (Any): An `adaptorHostEthIf` MO.

    Returns:
        str: UP, DOWN, DISABLED, or UNKNOWN for an unrecognized value.
    """
    return normalize_oper_state(getattr(mo, "operability", None))


def _admin_state(mo: Any) -> str:
    """
    Map UCS's administrative-state vocabulary onto the platform's.

    Args:
        mo (Any): Any MO carrying an `admin_state` property.

    Returns:
        str: ENABLED, DISABLED, or UNKNOWN for an unrecognized value.
    """
    return normalize_admin_state(getattr(mo, "admin_state", None))


def _attachments(
    adapter_ifs: Iterable[Any],
    *,
    provider_type: str,
    interface_kind: str,
    switches_by_id: dict[str, Any],
    cluster_name: str | None = None,
) -> tuple[ProviderAttachment, ...]:
    """
    Build fabric-interconnect attachments from adapter interfaces.

    Args:
        adapter_ifs (Iterable[Any]): Either every `adaptorExtEthIf` (the
            physical uplinks) or every `adaptorHostEthIf` (the OS-facing
            vNICs) for one server — never both in the same call, so every
            attachment this call produces gets the same `interface_kind`.
        provider_type (str): Which collector observed the attachment, not
            which product owns the fabric.
        interface_kind (str): `"PHYSICAL"` or `"VNIC"`, stamped onto every
            attachment produced from `adapter_ifs`.
        switches_by_id (dict[str, Any]): `networkElement` MOs keyed by
            `id` (`"A"`/`"B"`), from `ucs_manager.provider`'s domain-wide
            query. Supplies each attachment's `fabric_model`/
            `fabric_serial` — the two identifying facts UCS Manager
            actually exposes per Fabric Interconnect. See
            docs/cisco-collectors.md, "Adapter interfaces, MACs and
            fabric attachments".
        cluster_name (str | None): `topSystem.name`, the domain's own
            cluster name — shared by both FI-A and FI-B, since UCS
            Manager has no per-FI hostname. See ADR-0009's "Update
            (2026-09-07): `fabric_name` built and confirmed live".

    Returns:
        tuple[ProviderAttachment, ...]: One attachment per interface with a
            real `switch_id`; interfaces reporting none are skipped.

    See docs/cisco-collectors.md, "Adapter interfaces, MACs and fabric
    attachments", and "`operability` — a second vNIC signal ADR-0009 did
    not check" for why a `VNIC` attachment's `oper_state` comes from
    `_vnic_link_state` rather than `_oper_state`.
    """
    link_state_of = _vnic_link_state if interface_kind == "VNIC" else _oper_state
    attachments: list[ProviderAttachment] = []
    for mo in adapter_ifs:
        switch_id = getattr(mo, "switch_id", None)
        if not switch_id or switch_id.upper() == "NONE":
            continue
        switch = switches_by_id.get(switch_id)
        attachments.append(
            ProviderAttachment(
                type="FABRIC_INTERCONNECT",
                provider=provider_type,
                fabric=switch_id,
                fabric_name=cluster_name,
                fabric_id=None,
                fabric_model=getattr(switch, "model", None) if switch is not None else None,
                fabric_serial=getattr(switch, "serial", None) if switch is not None else None,
                server_interface=getattr(mo, "name", None) or getattr(mo, "id", None),
                server_port=None,
                fabric_port=getattr(mo, "peer_dn", None) or None,
                admin_state=_admin_state(mo),
                oper_state=link_state_of(mo),
                speed_mbps=None,
                interface_kind=interface_kind,
            )
        )
    return tuple(attachments)


def _cpu_model(cpu_units: list[Any]) -> str | None:
    """
    The processor model string for the server.

    Args:
        cpu_units (list[Any]): `processorUnit` MOs owned by one server, one
            per socket.

    Returns:
        str | None: The first equipped socket's model, or `None` when no
            socket is equipped or none reports a model.

    See docs/cisco-collectors.md, "CPU, memory and storage".
    """
    for mo in cpu_units:
        if is_equipped(mo):
            model = getattr(mo, "model", None)
            if model:
                return str(model)
    return None


_MEDIA_TYPE_MAP = {"hdd": "HDD", "ssd": "SSD", "nvme": "NVME"}

# Complete against the SDK's 20-value `DISK_STATE_*` enum; `NA`/`unknown`
# unmapped on purpose — ADR-0009, "Update (2026-09-07): the health/oper ...".
_DISK_HEALTH_MAP = {
    "good": "HEALTHY",
    "online": "HEALTHY",
    "unconfigured-good": "HEALTHY",
    "global-hot-spare": "HEALTHY",
    "dedicated-hot-spare": "HEALTHY",
    "jbod": "HEALTHY",
    "predictive-failure": "WARNING",
    "rebuilding": "WARNING",
    "copyback": "WARNING",
    "foreign-configuration": "WARNING",
    "locked-foreign-configuration": "WARNING",
    "zeroing": "WARNING",
    "bad": "CRITICAL",
    "failed": "CRITICAL",
    "unconfigured-bad": "CRITICAL",
    "disabled-for-removal": "CRITICAL",
    "offline": "CRITICAL",
    "self-test-failed": "CRITICAL",
}


def _media_type(mo: Any) -> str:
    """
    Map a disk's `device_type` onto the platform's `MediaType`.

    Args:
        mo (Any): A `storageLocalDisk` MO.

    Returns:
        str: HDD, SSD, NVME, or UNKNOWN.
    """
    return _MEDIA_TYPE_MAP.get(str(getattr(mo, "device_type", "") or "").lower(), "UNKNOWN")


def _disk_health(mo: Any) -> str:
    """
    Map a disk's `disk_state` onto the platform's `HealthSeverity`.

    Args:
        mo (Any): A `storageLocalDisk` MO.

    Returns:
        str: HEALTHY, WARNING, CRITICAL, or UNKNOWN for any state not
            worth distinguishing.
    """
    return _DISK_HEALTH_MAP.get(str(getattr(mo, "disk_state", "") or "").lower(), "UNKNOWN")


def _disk_capacity_bytes(mo: Any) -> int | None:
    """
    One disk's capacity in bytes.

    Args:
        mo (Any): A `storageLocalDisk` MO whose `size` is assumed to be MB.

    Returns:
        int | None: The capacity, or `None` when `size` is absent, the
            `not-applicable` sentinel, or unparseable.

    See docs/cisco-collectors.md, "CPU, memory and storage".
    """
    raw_size = getattr(mo, "size", None)
    if raw_size is None or str(raw_size).lower() == _NOT_APPLICABLE:
        return None
    size_mb = _as_int(raw_size)
    return size_mb * _BYTES_PER_MB if size_mb else None


def _storage_drives(disk_units: list[Any]) -> tuple[tuple[dict[str, object], ...], int]:
    """
    Summarize one server's local disks.

    Args:
        disk_units (list[Any]): `storageLocalDisk` MOs owned by one server.

    Returns:
        tuple[tuple[dict[str, object], ...], int]: `(drives, total_bytes)`
            over every equipped disk. A disk whose capacity could not be
            read still contributes a drive entry, with
            `capacity_bytes=None`, and adds nothing to the total rather
            than counting as zero.

    See docs/cisco-collectors.md, "CPU, memory and storage".
    """
    drives: list[dict[str, object]] = []
    total_bytes = 0
    for mo in disk_units:
        if not is_equipped(mo):
            continue
        capacity_bytes = _disk_capacity_bytes(mo)
        total_bytes += capacity_bytes or 0
        drives.append(
            {
                "id": str(mo.dn),
                "model": getattr(mo, "model", None) or None,
                "serial": getattr(mo, "serial", None) or None,
                "media_type": _media_type(mo),
                "capacity_bytes": capacity_bytes,
                "health": _disk_health(mo),
                "health_detail": getattr(mo, "disk_state", None) or None,
            }
        )
    return tuple(drives), total_bytes


_NOT_APPLICABLE_TEMP = "not-applicable"


def _gpu_temperature_celsius(mo: Any) -> float | None:
    """
    A GPU's reported temperature.

    A real sensor reading; the unit is assumed Celsius and unverified. See
    docs/cisco-collectors.md, "`temperature` — real telemetry".

    Args:
        mo (Any): A `graphicsCard` MO.

    Returns:
        float | None: The parsed value, or `None` for the
            `"not-applicable"` sentinel, an absent field, or anything
            unparseable.
    """
    raw = getattr(mo, "temperature", None)
    if raw is None or str(raw).strip().lower() == _NOT_APPLICABLE_TEMP:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _gpu(mo: Any) -> dict[str, object]:
    """
    One `graphicsCard` as the platform's GPU shape.

    `graphicsCard`, not `coprocessorCard` — see docs/cisco-collectors.md,
    "GPUs (coprocessor cards vs. graphics cards)".

    Args:
        mo (Any): A `graphicsCard` MO.

    Returns:
        dict[str, object]: Keys mirroring `app.domain.models.hardware.Gpu`.
            `memory_bytes`/`memory_type`/`ecc_mode_enabled`/error counts/
            `power_watts` are `None` by construction — no field for any
            of them exists on `graphicsCard` or `graphicsController`.
            `temperature_celsius` is real, unlike everywhere else in
            this platform's Cisco collectors — see
            `_gpu_temperature_celsius`.
    """
    return {
        "vendor": getattr(mo, "vendor", None) or None,
        "model": getattr(mo, "model", None) or None,
        "serial": getattr(mo, "serial", None) or None,
        "health": _oper_state(mo),
        "health_detail": getattr(mo, "oper_state", None) or None,
        "pci_address": getattr(mo, "pci_addr", None) or None,
        "firmware_version": getattr(mo, "firmware_version", None) or None,
        "memory_bytes": None,
        "memory_type": None,
        "ecc_mode_enabled": None,
        "correctable_error_count": None,
        "uncorrectable_error_count": None,
        "temperature_celsius": _gpu_temperature_celsius(mo),
        "power_watts": None,
    }


def _gpus(card_units: Iterable[Any]) -> tuple[dict[str, object], ...]:
    """
    Summarize one server's GPUs.

    Args:
        card_units (Iterable[Any]): `graphicsCard` MOs owned by one
            server.

    Returns:
        tuple[dict[str, object], ...]: One entry per equipped card. An
            unequipped slot is not reported, matching every other
            equipped-only collection in this module.

    See docs/cisco-collectors.md, "GPUs (coprocessor cards vs. graphics
    cards)".
    """
    return tuple(_gpu(mo) for mo in card_units if is_equipped(mo))


def _psu_wattage(mo: Any) -> int | None:
    """
    A PSU's rated capacity in watts.

    Args:
        mo (Any): An `equipmentPsu` MO.

    Returns:
        int | None: The parsed wattage, or `None` when absent or
            unparseable — not zero, which would read as a PSU rated for
            no power at all.
    """
    raw = getattr(mo, "psu_wattage", None)
    if raw is None:
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


def _psu_input_power(stat: Any | None) -> float | None:
    """
    A PSU's real-time input power draw in watts, from its stats child MO.

    See docs/cisco-collectors.md, "`capacity_watts` reading 0W live is
    `psu_wattage` being unpopulated, not a mapping bug".

    Args:
        stat (Any | None): The `equipmentRackUnitPsuStats` MO owned by
            this PSU, or `None` when it has none (blade chassis PSUs, or
            a rack unit with stats collection not enabled for this MO).

    Returns:
        float | None: The parsed wattage, or `None` when `stat` is
            `None`, absent, or unparseable.
    """
    if stat is None:
        return None
    raw = getattr(stat, "input_power", None)
    if raw is None:
        return None
    try:
        return float(str(raw))
    except ValueError:
        return None


def _psu(mo: Any, stat: Any | None = None) -> dict[str, object]:
    """
    One `equipmentPsu` as the platform's PSU shape.

    Args:
        mo (Any): An `equipmentPsu` MO.
        stat (Any | None): Its `equipmentRackUnitPsuStats` child, if any.

    Returns:
        dict[str, object]: Keys mirroring `app.domain.models.hardware.Psu`,
            plus `oper_power` — the MO's separate `power` field
            (ok/failed/error/off/degraded/...), collected alongside
            `oper_state` deliberately unreduced to one signal. Not part
            of the `Psu` domain model and not persisted — surfaced only
            in the dry-run print, so a live run can show whether
            `oper_state` or `power` tracks a real PSU failure more
            reliably before this settles on one. See
            docs/cisco-collectors.md, "Power supplies (PSUs)".
    """
    return {
        "id": getattr(mo, "id", None) or None,
        "model": getattr(mo, "model", None) or None,
        "serial": getattr(mo, "serial", None) or None,
        "health": _oper_state(mo),
        "health_detail": getattr(mo, "oper_state", None) or None,
        "capacity_watts": _psu_wattage(mo),
        "power_watts": _psu_input_power(stat),
        "oper_power": getattr(mo, "power", None) or None,
    }


def _psus(psu_units: Iterable[Any], psu_stats: Iterable[Any] = ()) -> tuple[dict[str, object], ...]:
    """
    Summarize one server's PSUs.

    Args:
        psu_units (Iterable[Any]): `equipmentPsu` MOs owned by one server.
        psu_stats (Iterable[Any]): `equipmentRackUnitPsuStats` MOs owned
            by the same server — each a child of one `equipmentPsu`,
            joined here by DN. Defaults to `()` for the same
            backward-compatibility reason `psu_units` itself does on
            `compute_unit_to_provider_server`.

    Returns:
        tuple[dict[str, object], ...]: One entry per equipped PSU slot.
            An unequipped slot is not reported at all — it is not a
            failed PSU, it is a bay this server was never populated
            with, and counting it would permanently misreport
            `power.failed_psu_count` for any server provisioned with
            fewer PSUs than bays. See docs/cisco-collectors.md, "Power
            supplies (PSUs)".
    """
    stats_by_psu_dn = {
        str(getattr(stat, "dn", "")).rsplit("/", 1)[0]: stat
        for stat in psu_stats
        if str(getattr(stat, "dn", "")).endswith("/rackunit-power-stats")
    }
    return tuple(
        _psu(mo, stats_by_psu_dn.get(getattr(mo, "dn", None)))
        for mo in psu_units
        if is_equipped(mo)
    )


def compute_unit_to_provider_server(
    server_mo: Any,
    *,
    manager_id: str,
    profile_by_dn: dict[str, Any],
    template_dn_by_name: dict[str, str],
    mgmt_if: Any | None,
    mgmt_ip_by_parent_dn: dict[str, Any],
    ext_eth_ifs: list[Any],
    host_eth_ifs: list[Any],
    cpu_units: list[Any],
    disk_units: list[Any],
    switches_by_id: dict[str, Any],
    psu_units: Iterable[Any] = (),
    psu_stats: Iterable[Any] = (),
    card_units: Iterable[Any] = (),
    provider_type: str = "UCS_MANAGER",
    cluster_name: str | None = None,
) -> ProviderServer:
    """
    Convert one compute unit and its descendants into a `ProviderServer`.

    Handles `computeBlade` and `computeRackUnit` alike, and serves both
    Cisco collectors unchanged — only `provider_type` differs between them.

    Args:
        server_mo (Any): The `computeBlade` or `computeRackUnit` MO.
        manager_id (str): The manager this server is reported under.
        profile_by_dn (dict[str, Any]): Service profile DN -> `lsServer`.
        template_dn_by_name (dict[str, str]): Template name -> DN.
        mgmt_if (Any | None): The server's own `mgmtIf`, or `None`.
        mgmt_ip_by_parent_dn (dict[str, Any]): Every domain
            `vnicIpV4PooledAddr`/`vnicIpV4StaticAddr` with a real address,
            from `ucs_common.management_ip_by_parent_dn`.
        ext_eth_ifs (list[Any]): Its `adaptorExtEthIf` MOs (physical
            uplinks).
        host_eth_ifs (list[Any]): Its `adaptorHostEthIf` MOs (OS-facing
            vNICs).
        cpu_units (list[Any]): Its `processorUnit` MOs.
        disk_units (list[Any]): Its `storageLocalDisk` MOs.
        switches_by_id (dict[str, Any]): Domain `networkElement` MOs
            keyed by `id` (`"A"`/`"B"`), passed through to `_attachments`.
        psu_units (Iterable[Any]): Its `equipmentPsu` MOs. Only populated
            for a rack-mount server — a blade's PSUs belong to its shared
            chassis, not to the blade, and `equipmentPsu` carries no
            relationship a blade could join through. Defaults to `()`
            rather than being required, unlike every other sub-resource
            argument here, so the many existing call sites that predate
            this field did not all need updating for it.
        psu_stats (Iterable[Any]): Its `equipmentRackUnitPsuStats` MOs —
            each a child of one `equipmentPsu` in `psu_units`, joined by
            DN in `_psus`. Defaults to `()` for the same reason
            `psu_units` does.
        card_units (Iterable[Any]): Its `graphicsCard` MOs (GPUs).
            Defaults to `()` for the same reason `psu_units` does.
        provider_type (str): Which collector observed this server.
        cluster_name (str | None): The domain's `topSystem.name`, passed
            through to `_attachments`'s `fabric_name`. Defaults to `None`
            for the same backward-compatibility reason `psu_units` does.

    Returns:
        ProviderServer: The vendor-neutral DTO the ingest pipeline consumes.

    See docs/cisco-collectors.md, "Shared object model and DN joins".
    """
    profile = profile_by_dn.get(getattr(server_mo, "assigned_to_dn", None) or "")
    template_name, template_external_id = _profile_template_fields(
        profile, template_dn_by_name=template_dn_by_name
    )
    mgmt_ip_addr = _management_ip_addr(
        profile=profile, server_mo=server_mo, mgmt_ip_by_parent_dn=mgmt_ip_by_parent_dn
    )

    total_memory_mb = _as_int(getattr(server_mo, "total_memory", None))
    storage_drives, storage_total_bytes = _storage_drives(disk_units)

    return ProviderServer(
        external_id=server_mo.dn,
        vendor="cisco",
        name=_server_name(server_mo, profile),
        model=getattr(server_mo, "model", None) or None,
        serial=getattr(server_mo, "serial", None) or None,
        system_uuid=getattr(server_mo, "uuid", None) or None,
        nic_macs=_nic_macs(host_eth_ifs=host_eth_ifs, ext_eth_ifs=ext_eth_ifs),
        nics=_nics(host_eth_ifs=host_eth_ifs, ext_eth_ifs=ext_eth_ifs),
        bmc_address_raw=_bmc_address(mgmt_if, mgmt_ip_addr),
        bmc_mac=getattr(mgmt_if, "mac", None) if mgmt_if is not None else None,
        manager_id=manager_id,
        profile_dn=profile.dn if profile is not None else None,
        profile_template_name=template_name,
        profile_template_external_id=template_external_id,
        cpu_sockets=_as_int(getattr(server_mo, "num_of_cpus", None)),
        cpu_cores=_as_int(getattr(server_mo, "num_of_cores", None)),
        cpu_threads=_as_int(getattr(server_mo, "num_of_threads", None)),
        cpu_model=_cpu_model(cpu_units),
        memory_total_bytes=total_memory_mb * _BYTES_PER_MB,
        storage_total_bytes=storage_total_bytes,
        storage_drives=storage_drives,
        psus=_psus(psu_units, psu_stats),
        gpus=_gpus(card_units),
        attachments=(
            _attachments(
                ext_eth_ifs,
                provider_type=provider_type,
                interface_kind="PHYSICAL",
                switches_by_id=switches_by_id,
                cluster_name=cluster_name,
            )
            + _attachments(
                host_eth_ifs,
                provider_type=provider_type,
                interface_kind="VNIC",
                switches_by_id=switches_by_id,
                cluster_name=cluster_name,
            )
        ),
        tags=(),
    )


def _as_int(value: object) -> int:
    """
    Coerce a UCS numeric attribute to `int`.

    UCS XML attributes arrive as strings and `ucsmsdk` does not coerce
    them, so every numeric field goes through this.

    Args:
        value (object): The raw attribute value, possibly `None`.

    Returns:
        int: The parsed value, or 0 when missing or non-numeric. Never
            raises — one unparseable count must not fail a whole server.
    """
    if value is None:
        return 0
    try:
        return int(str(value))
    except ValueError:
        return 0
