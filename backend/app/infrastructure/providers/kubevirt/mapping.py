"""Pure kubevirt-redfish payload -> `ProviderServer` mapping (docs/adr/0058).

No I/O. Reuses the standalone Redfish helpers where the facade serves the
same DMTF shape; what differs is identity (a VM has no SMBIOS serial unless
its firmware sets one) and link state (a stopped VM's NICs are not "down").
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.domain.enums import Vendor
from app.domain.ports.provider import ProviderNic, ProviderServer
from app.infrastructure.openshift.records import clean_hostname
from app.infrastructure.providers.redfish.mapping import (
    _clean_serial,
    cpu_summary,
    macs_from_interfaces,
    memory_bytes,
    memory_modules_from_dimms,
    nics_from_interfaces,
    storage_from_drives,
)

EXTERNAL_ID_SCHEME = "kubevirt://"
CONTROLLER_NAME = "VM Disks"
# Libvirt CPU modes, not models: what the guest gets depends on the node it lands on (ADR-0059).
_CPU_MODES = frozenset({"host-model", "host-passthrough"})


def external_id_for(netloc: str, odata_id: str) -> str:
    """
    The collector-native id of one VM.

    Args:
        netloc (str): The facade's `host[:port]`.
        odata_id (str): The System's `@odata.id`.

    Returns:
        str: `kubevirt://<host[:port]>/redfish/v1/Systems/<Id>`.
    """
    return f"{EXTERNAL_ID_SCHEME}{netloc}{odata_id}"


def serial_of(system: dict[str, Any]) -> str:
    """
    The correlation serial: the firmware serial, else the System `Id`.

    The `Id` is `<namespace>.<vm>` under the facade's `enhanced` convention,
    so it is stable for the VM's life and unique across chassis.

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.

    Returns:
        str: Never empty; `@odata.id` is the last resort.
    """
    return (
        _clean_serial(system.get("SerialNumber"))
        or str(system.get("Id") or "").strip()
        or str(system.get("@odata.id") or "")
    )


def cpu_fields(
    system: dict[str, Any], processors: list[dict[str, Any]] | None
) -> tuple[int | None, int | None, int | None, str | None]:
    """
    A VM's vCPU counts and model.

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.
        processors (list[dict[str, Any]] | None): `Processors` members; only
            fetched when `ProcessorSummary` is absent, else None.

    Returns:
        tuple[int | None, int | None, int | None, str | None]: As `cpu_summary`,
            except the model is `""` ("read, none") whenever the counts were read and
            the VM names no model, or only a CPU mode such as `host-model`.
    """
    sockets, cores, threads, model = cpu_summary(system, processors)
    if model is not None and model.strip().lower() in _CPU_MODES:
        model = ""
    if model is None and (isinstance(system.get("ProcessorSummary"), dict) or processors):
        model = ""
    return sockets, cores, threads, model


def host_node_of(system: dict[str, Any]) -> str | None:
    """
    The node a VM runs on, from the fork's `Oem.KubeVirt.NodeName` (ADR-0059).

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.

    Returns:
        str | None: The node, cleaned like a `nodes-status` hostname; `""` for a
            VM that is `Off`; `None` when a VM that is not off names no node
            (a facade build without the property).
    """
    oem = system.get("Oem")
    kubevirt = oem.get("KubeVirt") if isinstance(oem, dict) else None
    node = clean_hostname(kubevirt.get("NodeName") if isinstance(kubevirt, dict) else None)
    if node:
        return node
    return "" if system.get("PowerState") == "Off" else None


def nics_for(
    interfaces: list[dict[str, Any]] | None, power_state: object
) -> tuple[ProviderNic, ...]:
    """
    A VM's interfaces, with every link UNKNOWN unless the VM is running.

    The facade reports `NoLink` for a stopped VM; that is not a cable fault,
    so it must not read as CRITICAL "all links down" (ADR-0058).

    Args:
        interfaces (list[dict[str, Any]] | None): `EthernetInterfaces` members.
        power_state (object): `ComputerSystem.PowerState`.

    Returns:
        tuple[ProviderNic, ...]: One entry per interface.
    """
    nics = nics_from_interfaces(interfaces)
    if power_state == "On":
        return nics
    return tuple(replace(nic, link_state="UNKNOWN") for nic in nics)


def controllers_for(
    storage: list[dict[str, Any]] | None, drives: list[dict[str, Any]] | None
) -> tuple[dict[str, object], ...] | None:
    """
    The one virtual controller holding every disk.

    Args:
        storage (list[dict[str, Any]] | None): `Storage` members, None when unread.
        drives (list[dict[str, Any]] | None): Every `Drive` read, None when unread.

    Returns:
        tuple[dict[str, object], ...] | None: One controller per `Storage` member,
            `()` for an empty collection, None when either read failed.
    """
    if storage is None or drives is None:
        return None
    return tuple(
        {
            "id": str(member.get("Id") or "1"),
            "name": str(member.get("Name") or CONTROLLER_NAME),
            "kind": "OTHER",
            "drive_count": len(member.get("Drives") or []),
            "volumes": (),
        }
        for member in storage
    )


def system_to_provider_server(
    system: dict[str, Any],
    *,
    netloc: str,
    manager_id: str,
    cluster_name: str,
    processors: list[dict[str, Any]] | None,
    memory: list[dict[str, Any]] | None,
    storage: list[dict[str, Any]] | None,
    drives: list[dict[str, Any]] | None,
    interfaces: list[dict[str, Any]] | None,
) -> ProviderServer:
    """
    Convert one kubevirt-redfish System and its sub-resources into a `ProviderServer`.

    Args:
        system (dict[str, Any]): The `ComputerSystem` resource.
        netloc (str): The facade's `host[:port]`.
        manager_id (str): The manager this run reports under.
        cluster_name (str): `INVENTORY_KUBEVIRT_CLUSTER_NAME`, or "" when unset.
        processors (list[dict[str, Any]] | None): `Processors` members, or None.
        memory (list[dict[str, Any]] | None): `Memory` members, or None when unread.
        storage (list[dict[str, Any]] | None): `Storage` members, or None when unread.
        drives (list[dict[str, Any]] | None): Every `Drive`, or None when any was unread.
        interfaces (list[dict[str, Any]] | None): `EthernetInterfaces` members, or None.

    Returns:
        ProviderServer: Always reachable; no BMC MAC, PSU, GPU, template or RAID verdict.
    """
    odata_id = str(system.get("@odata.id") or "")
    sockets, cores, threads, cpu_model = cpu_fields(system, processors)
    storage_drives, storage_total = storage_from_drives(drives)
    model = system.get("Model")
    uuid = system.get("UUID")
    return ProviderServer(
        external_id=external_id_for(netloc, odata_id),
        vendor=Vendor.KUBEVIRT.value,
        name=str(system.get("Name") or system.get("Id") or odata_id).strip(),
        model=model.strip() if isinstance(model, str) and model.strip() else None,
        serial=serial_of(system),
        system_uuid=uuid.strip() if isinstance(uuid, str) and uuid.strip() else None,
        nic_macs=macs_from_interfaces(interfaces),
        nics=nics_for(interfaces, system.get("PowerState")),
        bmc_address_raw=f"redfish://{netloc}{odata_id}",
        bmc_mac=None,
        manager_id=manager_id,
        profile_template_name="",
        profile_template_external_id="",
        cpu_sockets=sockets,
        cpu_cores=cores,
        cpu_threads=threads,
        cpu_model=cpu_model,
        memory_total_bytes=memory_bytes(system, memory),
        memory_modules=memory_modules_from_dimms(memory),
        storage_total_bytes=storage_total,
        storage_drives=storage_drives,
        storage_controllers=controllers_for(storage, drives),
        raid_check=None,
        psus=(),
        gpus=(),
        attachments=(),
        tags=(),
        vm_host_cluster=cluster_name,
        vm_host_node=host_node_of(system),
    )
