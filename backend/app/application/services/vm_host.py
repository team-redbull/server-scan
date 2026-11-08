"""Resolve a VM's host node to the bare-metal server it is (docs/adr/0059-vm-host.md)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.models.server import Server, VmHost
from app.domain.ports.repository import ServerRepository


class HostMatch(StrEnum):
    """How a VM's host was resolved, from most to least certain."""

    MEMBERSHIP = "MEMBERSHIP"
    NAME = "NAME"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_IN_INVENTORY = "NOT_IN_INVENTORY"
    NOT_RUNNING = "NOT_RUNNING"


@dataclass(frozen=True, slots=True)
class VmHostResolution:
    """
    One VM's host, resolved at read time.

    Attributes:
        vm_host (VmHost): What the VM's record stores.
        match (HostMatch): How `host` was found, or why it was not.
        host (Server | None): The host's server record, on `MEMBERSHIP` or `NAME` only.
    """

    vm_host: VmHost
    match: HostMatch
    host: Server | None = None


async def resolve_vm_host(repo: ServerRepository, vm_host: VmHost) -> VmHostResolution:
    """
    Find the bare-metal server a VM runs on, through the cluster membership first.

    Args:
        repo (ServerRepository): Where servers are stored.
        vm_host (VmHost): The VM's stored host.

    Returns:
        VmHostResolution: The host when exactly one server matches.
    """
    if not vm_host.node_name:
        return VmHostResolution(vm_host=vm_host, match=HostMatch.NOT_RUNNING)
    candidates, by_membership = await repo.find_vm_host_candidates(
        vm_host.node_name, cluster_name=vm_host.cluster_name
    )
    if not candidates:
        return VmHostResolution(vm_host=vm_host, match=HostMatch.NOT_IN_INVENTORY)
    if len(candidates) > 1:
        return VmHostResolution(vm_host=vm_host, match=HostMatch.AMBIGUOUS)
    match = HostMatch.MEMBERSHIP if by_membership else HostMatch.NAME
    return VmHostResolution(vm_host=vm_host, match=match, host=candidates[0])
