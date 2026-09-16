"""Pure Redfish payload -> `ProviderServer` mapping.

No I/O: `provider.py` makes every request and hands this module plain
dicts. Every property path here was verified against the DMTF schema
bundle (2026.1) — see docs/adr/0016-redfish-standalone-collector.md for
what each was checked against and which schema version introduced it.

Two rules this module exists to enforce:

Nothing is required. Every resource's `required` list is only
`@odata.id`/`@odata.type`/`Id`/`Name`, so an absent property is normal
and never an error. A sub-resource the collector could not read is
reported as `None`, which the ingest pipeline carries forward rather than
overwriting good data with zeros.

No Redfish value is fed straight into one of this project's closed enums.
Redfish enums gain members between minor versions and vendors add their
own, so every crossing goes through an explicit map with a fallback.
Passing a vendor vocabulary through untouched is what silently disabled
the connectivity health signal for the whole Cisco fleet in ADR-0009.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import HealthSeverity, MediaType, Vendor
from app.domain.ports.provider import ProviderNic, ProviderServer

_MIB = 1024**2
_GIB = 1024**3

_VENDOR_PREFIXES: tuple[tuple[str, Vendor], ...] = (
    ("dell", Vendor.DELL),
    ("cisco", Vendor.CISCO),
    ("hpe", Vendor.HP),
    ("hewlett", Vendor.HP),
    ("hp ", Vendor.HP),
)

_HEALTH: dict[str, str] = {
    "OK": HealthSeverity.HEALTHY.value,
    "Warning": HealthSeverity.WARNING.value,
    "Critical": HealthSeverity.CRITICAL.value,
}

# SMBIOS placeholders, treated as no serial at all (ADR-0016, 2026-09-13 update).
_PLACEHOLDER_SERIALS = frozenset(
    {
        "",
        "0123456789",
        "default string",
        "to be filled by o.e.m.",
        "to be filled by o.e.m",
        "not specified",
        "none",
        "n/a",
        "unknown",
        "system serial number",
    }
)


def vendor_from_manufacturer(manufacturer: object) -> Vendor:
    """
    Map a Redfish `Manufacturer` onto this platform's vendor.

    Args:
        manufacturer (object): `ComputerSystem.Manufacturer` as received.

    Returns:
        Vendor: The matching vendor, or `Vendor.STANDALONE` both for a
            manufacturer this platform does not model and for one that is
            absent or null. The absent/null case was a collection failure
            through 2026-08-23 rather than a vendor decision — changed at
            the operator's request. See docs/adr/0016's dated update for
            the correlation-key risk this reopens: if the property starts
            reporting after ingesting under STANDALONE, the machine splits
            into two documents rather than one being corrected in place.
    """
    if not isinstance(manufacturer, str) or not manufacturer.strip():
        return Vendor.STANDALONE
    text = manufacturer.strip().lower()
    for prefix, vendor in _VENDOR_PREFIXES:
        if text.startswith(prefix):
            return vendor
    return Vendor.STANDALONE


def _clean_serial(raw: object) -> str | None:
    """
    A server's serial, with SMBIOS placeholders treated as absent.

    Args:
        raw (object): `SerialNumber` as received.

    Returns:
        str | None: The serial, or None when absent or a placeholder.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return None if text.lower() in _PLACEHOLDER_SERIALS else text


def _dell_serial(system: dict[str, Any]) -> str | None:
    """
    A Dell server's Service Tag, from the `DellSystem` OEM extension.

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.

    Returns:
        str | None: `Oem.Dell.DellSystem.NodeID`, or None when the system
            carries no Dell OEM block at all (every non-Dell vendor).
            Confirmed against several live iDRAC9 servers, 2026-09-08: the
            top-level `SerialNumber` this platform read until then is a
            manufacturing/board serial, not the Service Tag OME and the
            iDRAC UI show — `NodeID` is. See docs/dell-collectors.md.
    """
    oem = system.get("Oem")
    dell = oem.get("Dell") if isinstance(oem, dict) else None
    dell_system = dell.get("DellSystem") if isinstance(dell, dict) else None
    return _clean_serial(dell_system.get("NodeID")) if isinstance(dell_system, dict) else None


def _as_int(value: object) -> int | None:
    """
    Coerce a Redfish numeric property to `int`.

    Args:
        value (object): The raw value, possibly null, a float, or a string
            some firmware substituted for a number.

    Returns:
        int | None: The value, or None when absent or unparseable. Never
            raises — one unreadable count must not fail a whole server.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def health_of(resource: dict[str, Any]) -> str:
    """
    Map a resource's `Status.Health` onto a `HealthSeverity` value.

    Args:
        resource (dict[str, Any]): Any Redfish resource.

    Returns:
        str: HEALTHY, WARNING, CRITICAL, or UNKNOWN for anything else.
    """
    status = resource.get("Status")
    raw = status.get("Health") if isinstance(status, dict) else None
    return _HEALTH.get(str(raw), HealthSeverity.UNKNOWN.value)


def health_detail_of(resource: dict[str, Any]) -> str | None:
    """
    The raw `Status.Health` string `health_of` reduced to a `HealthSeverity`.

    Args:
        resource (dict[str, Any]): Any Redfish resource.

    Returns:
        str | None: The raw string, or `None` when `Status.Health` is
            absent or not a string.
    """
    status = resource.get("Status")
    raw = status.get("Health") if isinstance(status, dict) else None
    return raw if isinstance(raw, str) and raw else None


def is_absent(resource: dict[str, Any]) -> bool:
    """
    Report whether a component is physically not installed.

    `Status.State == "Absent"`, the analogue of `ucs_common.is_equipped`.

    Args:
        resource (dict[str, Any]): Any Redfish resource.

    Returns:
        bool: True when the resource reports itself absent.
    """
    status = resource.get("Status")
    return isinstance(status, dict) and str(status.get("State", "")) == "Absent"


def media_type_of(drive: dict[str, Any]) -> str:
    """
    Map a drive onto this platform's `MediaType`.

    NVMe lives on `Protocol`, not `MediaType` (ADR-0016, "Evidence").

    Args:
        drive (dict[str, Any]): A `Drive` resource.

    Returns:
        str: NVME, SSD, HDD, or UNKNOWN.
    """
    media = str(drive.get("MediaType", ""))
    protocol = str(drive.get("Protocol", ""))
    if protocol.upper() == "NVME":
        return MediaType.NVME.value
    if media == "SSD":
        return MediaType.SSD.value
    if media in ("HDD", "SMR"):
        return MediaType.HDD.value
    return MediaType.UNKNOWN.value


def drive_to_dict(drive: dict[str, Any]) -> dict[str, object]:
    """
    Convert one `Drive` resource into a `ProviderServer` drive entry.

    Args:
        drive (dict[str, Any]): A `Drive` resource.

    Returns:
        dict[str, object]: The drive as the ingest pipeline consumes it.
            `capacity_bytes` is None when unreadable — confirmed to occur
            on empty bays — and such a drive adds nothing to the total
            rather than counting as zero.
    """
    return {
        "id": str(drive.get("@odata.id") or drive.get("Id") or ""),
        "model": drive.get("Model") or None,
        "serial": drive.get("SerialNumber") or None,
        "media_type": media_type_of(drive),
        "capacity_bytes": _as_int(drive.get("CapacityBytes")),
        "health": health_of(drive),
        "health_detail": health_detail_of(drive),
    }


def storage_from_drives(
    drives: list[dict[str, Any]] | None,
) -> tuple[tuple[dict[str, object], ...] | None, int | None]:
    """
    Summarize a server's drives.

    Args:
        drives (list[dict[str, Any]] | None): Every `Drive` resource read,
            or None when the collector could not read them at all.

    Returns:
        tuple[tuple[dict[str, object], ...] | None, int | None]: The
            drives and their total capacity, or `(None, None)` when
            unread — which the ingest pipeline carries forward rather than
            overwriting.
    """
    if drives is None:
        return None, None
    entries: list[dict[str, object]] = []
    total = 0
    for drive in drives:
        if is_absent(drive):
            continue
        entry = drive_to_dict(drive)
        capacity = entry.get("capacity_bytes")
        if isinstance(capacity, int):
            total += capacity
        entries.append(entry)
    return tuple(entries), total


def cpu_summary(
    system: dict[str, Any], processors: list[dict[str, Any]] | None
) -> tuple[int | None, int | None, int | None, str | None]:
    """
    Resolve a server's CPU counts and model.

    `ProcessorSummary` first, falling back to summing `Processors` — a
    required fallback (ADR-0016, "Evidence").

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.
        processors (list[dict[str, Any]] | None): Its `Processors`
            members, or None when unread.

    Returns:
        tuple[int | None, int | None, int | None, str | None]:
            `(sockets, cores, threads, model)`, each None when neither the
            summary nor the fallback could supply it.
    """
    summary = system.get("ProcessorSummary")
    summary = summary if isinstance(summary, dict) else {}

    cpus = [p for p in (processors or []) if str(p.get("ProcessorType", "CPU")) == "CPU"]
    have_cpus = processors is not None and bool(cpus)

    sockets = _as_int(summary.get("Count"))
    if sockets is None and have_cpus:
        sockets = len(cpus)

    cores = _as_int(summary.get("CoreCount"))
    if cores is None and have_cpus:
        summed = [_as_int(p.get("TotalCores")) for p in cpus]
        cores = (
            sum(v for v in summed if v is not None) if any(v is not None for v in summed) else None
        )

    threads = _as_int(summary.get("LogicalProcessorCount"))
    if threads is None and have_cpus:
        summed = [_as_int(p.get("TotalThreads")) for p in cpus]
        threads = (
            sum(v for v in summed if v is not None) if any(v is not None for v in summed) else None
        )

    model = summary.get("Model") or None
    if not model and have_cpus:
        model = next((p.get("Model") for p in cpus if p.get("Model")), None)

    return sockets, cores, threads, str(model) if model else None


def is_gpu_processor(processor: dict[str, Any]) -> bool:
    """
    Report whether a `Processor` entry represents a GPU rather than a CPU.

    The single filter both this module and `provider.py` apply.

    Args:
        processor (dict[str, Any]): A `Processor` resource.

    Returns:
        bool: True when `ProcessorType == "GPU"` and the slot is not
            reported absent.
    """
    return str(processor.get("ProcessorType", "")) == "GPU" and not is_absent(processor)


def has_only_gpu_processors(processors: list[dict[str, Any]] | None) -> bool:
    """
    Report whether a `ComputerSystem` is a DGX/HGX GPU-baseboard tray.

    See ADR-0016's DGX/HGX update and its 2026-09-15 addendum.

    Args:
        processors (list[dict[str, Any]] | None): The system's
            `Processors` members, or None when unread.

    Returns:
        bool: True when at least one non-absent processor is a GPU and
            none is a CPU — a non-CPU, non-GPU companion (an FPGA,
            NVSwitch, ...) does not disqualify the tray. False for an
            empty or unread `Processors`, so a system this collector
            could not read is never assumed to be a tray.
    """
    if not processors:
        return False
    present = [p for p in processors if not is_absent(p)]
    has_gpu = any(is_gpu_processor(p) for p in present)
    # Same missing-key default as `cpu_summary`'s `has_cpus` — see ADR-0016's 2026-09-15 update.
    has_cpu = any(str(p.get("ProcessorType", "CPU")) == "CPU" for p in present)
    return has_gpu and not has_cpu


def _gpu_memory_type(processor: dict[str, Any]) -> str | None:
    """
    A GPU's memory generation, e.g. `"HBM3"`, `"HBM3e"`, `"GDDR6"`.

    Args:
        processor (dict[str, Any]): A `Processor` resource.

    Returns:
        str | None: The first `ProcessorMemory[].MemoryType` reported, or
            None when the array is absent or every entry omits it. A
            GPU's HBM stacks are uniform, so the first is representative.
    """
    banks = processor.get("ProcessorMemory")
    if not isinstance(banks, list):
        return None
    for bank in banks:
        if isinstance(bank, dict) and bank.get("MemoryType"):
            return str(bank["MemoryType"])
    return None


def _gpu_error_counts(metrics: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """
    A GPU's correctable and uncorrectable error counts.

    "Core" and "other" are summed — ADR-0016's GPU telemetry update says why.

    Args:
        metrics (dict[str, Any] | None): The GPU's own `ProcessorMetrics`
            resource, or None when unread.

    Returns:
        tuple[int | None, int | None]: `(correctable, uncorrectable)`,
            each None when no source field was numeric.
    """
    if metrics is None:
        return None, None
    correctable = [
        _as_int(metrics.get("CorrectableCoreErrorCount")),
        _as_int(metrics.get("CorrectableOtherErrorCount")),
    ]
    uncorrectable = [
        _as_int(metrics.get("UncorrectableCoreErrorCount")),
        _as_int(metrics.get("UncorrectableOtherErrorCount")),
    ]
    present_c = [v for v in correctable if v is not None]
    present_u = [v for v in uncorrectable if v is not None]
    return (sum(present_c) if present_c else None), (sum(present_u) if present_u else None)


def _sensor_reading(container: dict[str, Any] | None, key: str) -> float | None:
    """
    Read a Redfish `SensorExcerpt`-shaped property's nested `.Reading`.

    See ADR-0016's 2026-09-13 update.

    Args:
        container (dict[str, Any] | None): The `EnvironmentMetrics`
            resource, or None when unread.
        key (str): The sensor property name, e.g. `"TemperatureCelsius"`.

    Returns:
        float | None: The reading, or None when absent, unread, or
            non-numeric.
    """
    if container is None:
        return None
    sensor = container.get(key)
    if not isinstance(sensor, dict):
        return None
    reading = sensor.get("Reading")
    if reading is None:
        return None
    try:
        return float(reading)
    except (TypeError, ValueError):
        return None


def gpus_from_processors(
    processors: list[dict[str, Any]] | None,
    *,
    metrics_by_processor: dict[str, dict[str, Any]] | None = None,
    environment_by_processor: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, object], ...] | None:
    """
    Extract GPUs (`ProcessorType == "GPU"`) from a server's `Processors`.

    VRAM is `MemorySummary.TotalMemorySizeMiB` — MiB, not GiB — and coverage
    is best-effort (ADR-0016, "Evidence" and "What is still unproven").

    Args:
        processors (list[dict[str, Any]] | None): `Processors` members, or
            None when unread.
        metrics_by_processor (dict[str, dict[str, Any]] | None): Each
            GPU's own `ProcessorMetrics` resource, keyed by the
            processor's `@odata.id`. Empty/missing entries degrade to
            unread rather than failing the GPU.
        environment_by_processor (dict[str, dict[str, Any]] | None): Each
            GPU's own `EnvironmentMetrics` resource, keyed the same way.

    Returns:
        tuple[dict[str, object], ...] | None: One entry per GPU, or None
            when the collection could not be read.
    """
    if processors is None:
        return None
    metrics_by_processor = metrics_by_processor or {}
    environment_by_processor = environment_by_processor or {}
    gpus: list[dict[str, object]] = []
    for processor in processors:
        if not is_gpu_processor(processor):
            continue
        summary = processor.get("MemorySummary")
        summary = summary if isinstance(summary, dict) else {}
        mib = _as_int(summary.get("TotalMemorySizeMiB"))
        if mib is None:
            # Pre-2020.4 firmware has no `MemorySummary` on a `Processor`.
            banks = processor.get("ProcessorMemory")
            if isinstance(banks, list):
                sizes = [_as_int(b.get("CapacityMiB")) for b in banks if isinstance(b, dict)]
                present = [s for s in sizes if s is not None]
                mib = sum(present) if present else None
        processor_id = str(processor.get("@odata.id") or "")
        correctable, uncorrectable = _gpu_error_counts(metrics_by_processor.get(processor_id))
        environment = environment_by_processor.get(processor_id)
        ecc_enabled = summary.get("ECCModeEnabled")
        gpus.append(
            {
                "vendor": processor.get("Manufacturer") or None,
                "model": processor.get("Model") or None,
                "serial": processor.get("SerialNumber") or None,
                "memory_bytes": mib * _MIB if mib is not None else None,
                "health": health_of(processor),
                "health_detail": health_detail_of(processor),
                "pci_address": None,
                "firmware_version": processor.get("FirmwareVersion") or None,
                "memory_type": _gpu_memory_type(processor),
                "ecc_mode_enabled": ecc_enabled if isinstance(ecc_enabled, bool) else None,
                "correctable_error_count": correctable,
                "uncorrectable_error_count": uncorrectable,
                "temperature_celsius": _sensor_reading(environment, "TemperatureCelsius"),
                "power_watts": _sensor_reading(environment, "PowerWatts"),
            }
        )
    return tuple(gpus)


# PCI-SIG vendor IDs matched against `Manufacturer`'s leading 4 hex
# digits — see ADR-0016's 2026-09-15 PCIeDevice update for which are confirmed live.
_GPU_PCI_VENDOR_IDS: dict[str, str] = {"10DE": "NVIDIA", "1002": "AMD", "8086": "Intel"}

_GPU_DESCRIPTION_HINTS = ("VGA", "GPU", "3D", "DISPLAY")

# (vendor_id, device_id) -> a string `GpuCatalog` already has as an
# alias — see ADR-0016's 2026-09-15/2026-09-16 GPU-model updates.
_BUILTIN_PCI_DEVICE_MODELS: dict[tuple[str, str], str] = {
    ("10de", "15f7"): "Tesla P100-PCIE-12GB",
    ("10de", "15f8"): "Tesla P100-PCIE-16GB",
    ("10de", "1eb8"): "Tesla T4",
    ("10de", "20b0"): "A100-SXM4-40GB",
    ("10de", "20b1"): "A100-PCIE-40GB",
    ("10de", "20b2"): "A100-SXM4-80GB",
    ("10de", "20b5"): "A100-PCIE-80GB",
    ("10de", "20b7"): "A30",
    ("10de", "20bd"): "A800-SXM4-40GB",
    ("10de", "20f3"): "A800-SXM4-80GB",
    ("10de", "2235"): "A40",
    ("10de", "2236"): "A10",
    ("10de", "2237"): "A10G",
    ("10de", "2322"): "H800 PCIe",
    ("10de", "2324"): "H800",
    ("10de", "2330"): "H100-SXM5-80GB",
    ("10de", "2331"): "H100 PCIe",
}


class PcieGpuModelSpecError(ValueError):
    """`INVENTORY_REDFISH_PCIE_GPU_MODELS` is malformed."""


def parse_pcie_gpu_models(spec: str) -> dict[tuple[str, str], str]:
    """
    Parse `INVENTORY_REDFISH_PCIE_GPU_MODELS` into PCI ID -> model overrides.

    `"vendor_id:device_id:Model Name"` triples, comma-separated. See
    ADR-0016's 2026-09-16 update.

    Args:
        spec (str): The raw env var value; empty adds nothing.

    Returns:
        dict[tuple[str, str], str]: Lowercase `(vendor_id, device_id)` ->
            model, merged over `_BUILTIN_PCI_DEVICE_MODELS` by the caller
            so an operator entry overrides a built-in one with the same
            ID, never the reverse.

    Raises:
        PcieGpuModelSpecError: On a malformed entry.
    """
    overrides: dict[tuple[str, str], str] = {}
    for raw_entry in spec.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3:
            raise PcieGpuModelSpecError(
                f"INVENTORY_REDFISH_PCIE_GPU_MODELS: {entry!r} is not "
                "'vendor_id:device_id:Model Name' — expected exactly two ':'."
            )
        vendor_id, device_id, model = (p.strip() for p in parts)
        for label, value in (("vendor_id", vendor_id), ("device_id", device_id)):
            if len(value) != 4:
                raise PcieGpuModelSpecError(
                    f"INVENTORY_REDFISH_PCIE_GPU_MODELS: {entry!r}'s {label} "
                    f"{value!r} must be exactly 4 hex digits."
                )
            try:
                int(value, 16)
            except ValueError as exc:
                raise PcieGpuModelSpecError(
                    f"INVENTORY_REDFISH_PCIE_GPU_MODELS: {entry!r}'s {label} "
                    f"{value!r} is not valid hex."
                ) from exc
        if not model:
            raise PcieGpuModelSpecError(
                f"INVENTORY_REDFISH_PCIE_GPU_MODELS: {entry!r} names no model — the "
                "part after the second ':'."
            )
        overrides[(vendor_id.lower(), device_id.lower())] = model
    return overrides


def _pci_ids_from_manufacturer(manufacturer: str) -> tuple[str, str] | None:
    """
    Split a `Manufacturer` that packs a raw PCI vendor+device ID as 8 hex digits.

    See ADR-0016's 2026-09-15 GPU-model update.

    Args:
        manufacturer (str): The raw `Manufacturer` string.

    Returns:
        tuple[str, str] | None: `(vendor_id, device_id)`, both lowercase
            4-hex-digit strings, or None when it isn't 8 hex digits.
    """
    candidate = manufacturer.strip()
    if len(candidate) != 8:
        return None
    try:
        int(candidate, 16)
    except ValueError:
        return None
    return candidate[:4].lower(), candidate[4:].lower()


def pcie_device_refs(system: dict[str, Any]) -> tuple[str, ...]:
    """
    A `ComputerSystem`'s own `PCIeDevices` links, a direct array — not a collection.

    Distinct from `Chassis.PCIeDevices`, which links to a real
    `PCIeDeviceCollection` instead. See ADR-0016's 2026-09-15 update.

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.

    Returns:
        tuple[str, ...]: Every entry's `@odata.id`, or `()` when the
            property is absent or empty.
    """
    entries = system.get("PCIeDevices")
    if not isinstance(entries, list):
        return ()
    return tuple(
        odata_id
        for entry in entries
        if isinstance(entry, dict) and isinstance(odata_id := entry.get("@odata.id"), str)
    )


def is_gpu_pcie_device(device: dict[str, Any]) -> bool:
    """
    Report whether a `PCIeDevice` looks like a GPU, by vendor ID and description.

    See ADR-0016's 2026-09-15 PCIeDevice update.

    Args:
        device (dict[str, Any]): A `PCIeDevice` resource.

    Returns:
        bool: True when `Manufacturer` starts with a known GPU vendor's
            PCI-SIG ID and `Description` names a display/GPU-shaped
            device. Neither alone is trusted: a vendor ID can belong to
            a non-GPU card, and a description could plausibly mention a
            device type by coincidence.
    """
    if is_absent(device):
        return False
    manufacturer = str(device.get("Manufacturer") or "").upper()
    if manufacturer[:4] not in _GPU_PCI_VENDOR_IDS:
        return False
    description = str(device.get("Description") or "").upper()
    return any(hint in description for hint in _GPU_DESCRIPTION_HINTS)


def _pcie_address(device: dict[str, Any]) -> str | None:
    """
    A PCIe device's location, from the last segment of its own `@odata.id`.

    Args:
        device (dict[str, Any]): A `PCIeDevice` resource.

    Returns:
        str | None: The trailing path segment (e.g. `"00_4E_00"`), or
            None when `@odata.id` is absent or malformed.
    """
    odata_id = device.get("@odata.id")
    if not isinstance(odata_id, str) or not odata_id:
        return None
    return odata_id.rsplit("/", 1)[-1] or None


def pcie_device_to_gpu(
    device: dict[str, Any],
    *,
    pci_device_models: dict[tuple[str, str], str] = _BUILTIN_PCI_DEVICE_MODELS,
) -> dict[str, object]:
    """
    One `PCIeDevice` as the platform's GPU shape — the fallback for a BMC with no `Processor` GPUs.

    See ADR-0016's 2026-09-15 PCIeDevice update for the fields this
    resource does and does not carry.

    Args:
        device (dict[str, Any]): A `PCIeDevice` already confirmed a GPU
            by `is_gpu_pcie_device`.
        pci_device_models (dict[tuple[str, str], str]): `(vendor_id,
            device_id)` -> a `GpuCatalog`-matchable model name. Defaults
            to the built-in table; the provider passes one already
            merged with `INVENTORY_REDFISH_PCIE_GPU_MODELS`.

    Returns:
        dict[str, object]: Keys mirroring `app.domain.models.hardware.Gpu`.
            `model` is a `GpuCatalog`-matchable name when its PCI ID is
            in `pci_device_models`, so `VRAM` fills in downstream at
            ingest the same way it does for any other vendor's GPU — the
            raw `Description` (`"10DE VGA"`) otherwise. Every telemetry
            field a `Processor`-reported GPU can carry (`memory_bytes`,
            `ecc_mode_enabled`, error counts, `temperature_celsius`,
            `power_watts`) is still `None` by construction — `PCIeDevice`
            itself has no such properties.
    """
    manufacturer = str(device.get("Manufacturer") or "")
    pci_ids = _pci_ids_from_manufacturer(manufacturer)
    vendor = _GPU_PCI_VENDOR_IDS.get(manufacturer[:4].upper())
    model = device.get("Description") or None
    if pci_ids is not None:
        model = pci_device_models.get(pci_ids, model)
    return {
        "vendor": vendor,
        "model": model,
        "serial": None,
        "memory_bytes": None,
        "health": health_of(device),
        "health_detail": health_detail_of(device),
        "pci_address": _pcie_address(device),
        "firmware_version": None,
        "memory_type": None,
        "ecc_mode_enabled": None,
        "correctable_error_count": None,
        "uncorrectable_error_count": None,
        "temperature_celsius": None,
        "power_watts": None,
    }


def gpus_from_pcie_devices(
    devices: list[dict[str, Any]] | None,
    *,
    max_gpus: int,
    pci_device_models: dict[tuple[str, str], str] = _BUILTIN_PCI_DEVICE_MODELS,
) -> tuple[dict[str, object], ...] | None:
    """
    Screen `ComputerSystem.PCIeDevices` for GPUs — the fallback when `Processors` reports none.

    See ADR-0016's 2026-09-15 PCIeDevice update.

    Args:
        devices (list[dict[str, Any]] | None): `PCIeDevices` members, or
            None when unread.
        max_gpus (int): Stop once this many GPUs are found, bounding a
            chassis with hundreds of unrelated PCIe functions.
        pci_device_models (dict[tuple[str, str], str]): Passed through to
            `pcie_device_to_gpu` — see its own docstring.

    Returns:
        tuple[dict[str, object], ...] | None: One entry per matched GPU,
            in encounter order, or None when `devices` itself is unread.
    """
    if devices is None:
        return None
    gpus: list[dict[str, object]] = []
    for device in devices:
        if len(gpus) >= max_gpus:
            break
        if is_gpu_pcie_device(device):
            gpus.append(pcie_device_to_gpu(device, pci_device_models=pci_device_models))
    return tuple(gpus)


def macs_from_interfaces(interfaces: list[dict[str, Any]] | None) -> tuple[str, ...] | None:
    """
    Pull MACs off a server's `EthernetInterfaces`.

    Args:
        interfaces (list[dict[str, Any]] | None): The members, or None
            when the collection could not be read.

    Returns:
        tuple[str, ...] | None: One MAC per interface that reports one,
            preferring `MACAddress` over `PermanentMACAddress`; None when
            unread.
    """
    if interfaces is None:
        return None
    macs: list[str] = []
    for interface in interfaces:
        mac = interface.get("MACAddress") or interface.get("PermanentMACAddress")
        if isinstance(mac, str) and mac.strip():
            macs.append(mac.strip())
    return tuple(macs)


# `EthernetInterface.LinkStatus` onto `LinkState` (ADR-0016, 2026-09-13 update).
_LINK_STATUS = {
    "LinkUp": "UP",
    "Up": "UP",
    "LinkDown": "DOWN",
    "Down": "DOWN",
    "NoLink": "DOWN",
}


# `Psu.health` vocabulary, never `HealthSeverity` — ADR-0016, 2026-09-13 update.
_PSU_STATE = {
    "Enabled": "UP",
    "StandbyOffline": "UP",
    "StandbySpare": "UP",
    "Disabled": "DISABLED",
    "UnavailableOffline": "DOWN",
}


def psu_health(supply: dict[str, Any]) -> str:
    """
    One power supply's state, in the platform's UP/DOWN/DISABLED/UNKNOWN vocabulary.

    `Status.Health` decides first (ADR-0016, 2026-09-13 update). Public
    because `..oneview.mapping.psus_from` reuses it as its fallback.

    Args:
        supply (dict[str, Any]): One `PowerSupply` resource.

    Returns:
        str: UP, DOWN, DISABLED, or UNKNOWN.
    """
    status = supply.get("Status")
    status = status if isinstance(status, dict) else {}
    health = str(status.get("Health") or "")
    if health == "Critical":
        return "DOWN"
    if health == "Warning":
        return "UNKNOWN"
    state = str(status.get("State") or "")
    if health == "OK" and state not in _PSU_STATE:
        return "UP"
    return _PSU_STATE.get(state, "UNKNOWN")


def psus_from_supplies(
    supplies: list[dict[str, Any]] | None,
    *,
    metrics_by_supply: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, object], ...] | None:
    """
    Map a chassis's power supplies onto the platform's PSU shape.

    See ADR-0016's 2026-09-15 PSU telemetry update for `power_watts`.

    Args:
        supplies (list[dict[str, Any]] | None): `PowerSupply` resources,
            from either schema generation, or None when unread.
        metrics_by_supply (dict[str, dict[str, Any]] | None): Each
            supply's own `PowerSupplyMetrics`, keyed by the supply's
            `@odata.id`. Empty/missing entries degrade to unread rather
            than failing the PSU.

    Returns:
        tuple[dict[str, object], ...] | None: One entry per fitted supply,
            with keys mirroring `app.domain.models.hardware.Psu` plus
            `redfish_status` — the raw `Health`/`State` pair, carried
            unreduced for the dry-run print the way the Cisco mapping
            carries `oper_power`. Not persisted. None propagates "unread",
            which `_carry_forward` needs to keep stored PSUs on a run that
            could not reach the chassis.
    """
    if supplies is None:
        return None
    metrics_by_supply = metrics_by_supply or {}
    psus: list[dict[str, object]] = []
    for supply in supplies:
        if is_absent(supply):
            continue
        status = supply.get("Status")
        status = status if isinstance(status, dict) else {}
        raw_status = f"{status.get('Health') or '—'}/{status.get('State') or '—'}"
        metrics = metrics_by_supply.get(str(supply.get("@odata.id") or ""))
        psus.append(
            {
                "id": str(supply.get("MemberId") or supply.get("Id") or supply.get("Name") or "")
                or None,
                "model": supply.get("Model") or None,
                "serial": supply.get("SerialNumber") or None,
                "health": psu_health(supply),
                "health_detail": raw_status,
                "capacity_watts": _as_int(
                    supply.get("PowerCapacityWatts") or supply.get("CapacityWatts")
                ),
                "power_watts": _sensor_reading(metrics, "InputPowerWatts"),
                "redfish_status": raw_status,
            }
        )
    return tuple(psus)


def memory_modules_from_dimms(
    dimms: list[dict[str, Any]] | None,
) -> tuple[dict[str, object], ...] | None:
    """
    Map the `Memory` collection onto the platform's DIMM shape.

    Absent slots are dropped.

    Args:
        dimms (list[dict[str, Any]] | None): `Memory` members, or None
            when the collection could not be read.

    Returns:
        tuple[dict[str, object], ...] | None: One entry per fitted DIMM,
            keys mirroring `app.domain.models.hardware.MemoryModule`, or
            None when unread. `health` is a `HealthSeverity` value, the
            vocabulary drives use — a DIMM is reported as a health rollup
            everywhere, never as an operational state.
    """
    if dimms is None:
        return None
    modules: list[dict[str, object]] = []
    for dimm in dimms:
        if is_absent(dimm):
            continue
        capacity_mib = _as_int(dimm.get("CapacityMiB"))
        modules.append(
            {
                "slot": dimm.get("DeviceLocator") or dimm.get("Id") or dimm.get("Name") or None,
                "size_bytes": capacity_mib * _MIB if capacity_mib is not None else None,
                "type": dimm.get("MemoryDeviceType") or dimm.get("MemoryType") or None,
                "speed_mhz": _as_int(dimm.get("OperatingSpeedMhz")),
                "serial": dimm.get("SerialNumber") or None,
                "health": health_of(dimm),
            }
        )
    return tuple(modules)


def nics_from_interfaces(interfaces: list[dict[str, Any]] | None) -> tuple[ProviderNic, ...]:
    """
    Build the per-interface view from a server's `EthernetInterfaces`.

    This populates `NetworkInfo.interfaces`; `macs_from_interfaces` stays
    the identity correlation key.

    Args:
        interfaces (list[dict[str, Any]] | None): The `EthernetInterfaces`
            members, or None when the collection could not be read.

    Returns:
        tuple[ProviderNic, ...]: One entry per interface, named by `Name`
            then `Id`, and located by `Id`. Empty when the collection was
            unread or empty — `ProviderServer.nics` has no "not read" state,
            and the MAC set beside it already carries that distinction.
    """
    if not interfaces:
        return ()
    nics: list[ProviderNic] = []
    for interface in interfaces:
        mac = interface.get("MACAddress") or interface.get("PermanentMACAddress")
        speed = interface.get("SpeedMbps")
        # `Id` is the FQDD on iDRAC — docs/dell-collectors.md, "NICs".
        identifier = str(interface.get("Id") or "").strip()
        nics.append(
            ProviderNic(
                name=str(interface.get("Name") or identifier or "").strip(),
                mac=mac.strip() if isinstance(mac, str) and mac.strip() else None,
                speed_mbps=(
                    speed if isinstance(speed, int) and not isinstance(speed, bool) else None
                ),
                link_state=_LINK_STATUS.get(str(interface.get("LinkStatus") or ""), "UNKNOWN"),
                location=identifier or None,
            )
        )
    return tuple(nics)


def memory_bytes(system: dict[str, Any], dimms: list[dict[str, Any]] | None) -> int | None:
    """
    A server's total memory, in bytes.

    `MemorySummary.TotalSystemMemoryGiB` (a `number`, rounded here), else the
    sum of `Memory[].CapacityMiB` — a required fallback (ADR-0016, 2026-08-23).

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.
        dimms (list[dict[str, Any]] | None): Its `Memory` collection
            members, or None when unread.

    Returns:
        int | None: Total memory in bytes, or None when neither source
            could supply it.
    """
    summary = system.get("MemorySummary")
    raw = summary.get("TotalSystemMemoryGiB") if isinstance(summary, dict) else None
    if raw is not None:
        try:
            return round(float(raw) * _GIB)
        except (TypeError, ValueError):
            pass
    if dimms is None:
        return None
    sizes = [_as_int(d.get("CapacityMiB")) for d in dimms if not is_absent(d)]
    present = [s for s in sizes if s is not None]
    return sum(present) * _MIB if present else None


def system_to_provider_server(
    system: dict[str, Any],
    *,
    host: str,
    base_url: str,
    manager_id: str,
    override_name: str | None,
    processors: list[dict[str, Any]] | None,
    drives: list[dict[str, Any]] | None,
    dimms: list[dict[str, Any]] | None,
    interfaces: list[dict[str, Any]] | None,
    bmc_mac: str | None,
    psus: tuple[dict[str, object], ...] | None = None,
    gpu_metrics_by_processor: dict[str, dict[str, Any]] | None = None,
    gpu_environment_by_processor: dict[str, dict[str, Any]] | None = None,
    extra_gpus: tuple[dict[str, object], ...] = (),
    chassis_product_name: str | None = None,
) -> ProviderServer:
    """
    Convert one `ComputerSystem` and its sub-resources into a `ProviderServer`.

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.
        host (str): The address the operator listed this BMC under.
        base_url (str): That host's origin, for `bmc_address_raw`.
        manager_id (str): The manager this run reports under.
        override_name (str | None): An operator-supplied name, preferred
            over anything the BMC reports.
        processors (list[dict[str, Any]] | None): `Processors` members.
        drives (list[dict[str, Any]] | None): Every `Drive` read.
        dimms (list[dict[str, Any]] | None): `Memory` collection members
            — one per installed DIMM.
        interfaces (list[dict[str, Any]] | None): `EthernetInterfaces`
            members.
        bmc_mac (str | None): The BMC's own MAC.
        psus (tuple[dict[str, object], ...] | None): Fitted power supplies
            from this system's chassis, or None when unread.
        gpu_metrics_by_processor (dict[str, dict[str, Any]] | None): Each
            GPU processor's own `ProcessorMetrics`, keyed by `@odata.id`.
        gpu_environment_by_processor (dict[str, dict[str, Any]] | None):
            Each GPU processor's own `EnvironmentMetrics`, keyed the same
            way.
        extra_gpus (tuple[dict[str, object], ...]): Already-mapped GPU
            entries from a sibling GPU-baseboard system being merged
            into this one — see `has_only_gpu_processors`. Appended
            after this system's own GPUs, if any.
        chassis_product_name (str | None): The owning chassis's own
            `ProductName`, used only when `Model` is blank.

    Returns:
        ProviderServer: The vendor-neutral DTO the ingest pipeline
            consumes. Never raises on a missing `Manufacturer` — see
            `vendor_from_manufacturer`, which maps that to
            `Vendor.STANDALONE` rather than failing the system.
    """
    vendor = vendor_from_manufacturer(system.get("Manufacturer"))

    odata_id = str(system.get("@odata.id", ""))
    sockets, cores, threads, cpu_model = cpu_summary(system, processors)
    storage_drives, storage_total = storage_from_drives(drives)

    gpus = gpus_from_processors(
        processors,
        metrics_by_processor=gpu_metrics_by_processor,
        environment_by_processor=gpu_environment_by_processor,
    )
    if extra_gpus:
        gpus = (gpus or ()) + extra_gpus

    return ProviderServer(
        external_id=f"redfish://{host}{odata_id}",
        vendor=vendor.value,
        name=override_name or _server_name(system),
        model=_model(system, chassis_product_name),
        serial=_dell_serial(system) or _clean_serial(system.get("SerialNumber")),
        system_uuid=system.get("UUID") or None,
        nic_macs=macs_from_interfaces(interfaces),
        nics=nics_from_interfaces(interfaces),
        # Never from the operator's raw string — ADR-0016, 2026-09-13 update.
        bmc_address_raw=f"{base_url.replace('https://', 'redfish://')}{odata_id}",
        bmc_mac=bmc_mac,
        manager_id=manager_id,
        cpu_sockets=sockets,
        cpu_cores=cores,
        cpu_threads=threads,
        cpu_model=cpu_model,
        memory_total_bytes=memory_bytes(system, dimms),
        storage_total_bytes=storage_total,
        storage_drives=storage_drives,
        memory_modules=memory_modules_from_dimms(dimms),
        psus=psus,
        gpus=gpus,
        # No fabric interconnect to attach to — ADR-0016's Decision.
        attachments=(),
        tags=(),
    )


def _model(system: dict[str, Any], chassis_product_name: str | None) -> str | None:
    """
    A server's model, falling back to `Chassis.ProductName` when blank.

    Confirmed live — ADR-0016's 2026-09-16 update.

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.
        chassis_product_name (str | None): The owning chassis's
            `ProductName`, already cleaned, or None.

    Returns:
        str | None: The model, or None when neither source has one.
    """
    value = system.get("Model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return chassis_product_name


def _server_name(system: dict[str, Any]) -> str:
    """
    The name an operator would use for this machine.

    `HostName`, else the stable `Name`/`Id` (ADR-0016, 2026-09-13 update).

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.

    Returns:
        str: The best available name.
    """
    for key in ("HostName", "Name", "Id"):
        value = system.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(system.get("@odata.id", "unknown"))
