"""`Server` -> flat facts dict.

The one place that reaches into the nested `Server` document shape.
Everything downstream (the metric registry's resolvers, condition
evaluation) works against this flat dict, not the domain model — so a
later change to how hardware is nested on `Server` touches this one
function, not every policy evaluation path.

What each fact counts, and the live-data bug behind each choice, is in
docs/architecture.md, "Health policy engine" ("The facts vocabulary").
Every fact counts only definite readings — never UNKNOWN (ADR-0027). Each
`<category>.has_data` says whether anything was read for it at all; the
engine judges no policy in a category without it (ADR-0027, 2026-09-21).
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.enums import HealthSeverity
from app.domain.models.server import Server

# Failed, in either vocabulary: Redfish `HealthSeverity` or Cisco oper-state.
_FAILED = frozenset({"CRITICAL", "DOWN"})

# Degraded or dead, counted together for the disk and DIMM checks.
_NOT_GOOD = frozenset({"CRITICAL", "WARNING", "DOWN"})

# Storage builds only the server's name records; one boolean fact each.
_STORAGE_NAME_TOKENS = {"server.name_has_10tb": "10tb", "server.name_has_5tb": "5tb"}


def _has_name_token(name: str | None, token: str) -> bool:
    """
    Whether `name` carries `token` as a whole `-`-delimited segment.

    Not a substring check: `"35tb"` must never match `"5tb"` (a real
    35TB-class server, 2026-09-17).

    Args:
        name (str | None): The server's name.
        token (str): The lowercase token to look for, e.g. `"5tb"`.

    Returns:
        bool: Whether one of `name`'s hyphen-delimited segments is exactly `token`.
    """
    return token in (name or "").lower().split("-")


_CAPACITY_TOKEN = re.compile(r"^(\d+)tb$")


def _name_capacity_bytes(name: str | None) -> int | None:
    """
    The nominal capacity a `-<N>tb` name segment promises, in decimal bytes.

    Any whole segment, not just `"5tb"`/`"10tb"` — ADR update, 2026-09-17.

    Args:
        name (str | None): The server's name.

    Returns:
        int | None: `N * 10**12` bytes, or None if no segment matches.
    """
    for segment in (name or "").lower().split("-"):
        match = _CAPACITY_TOKEN.match(segment)
        if match:
            return int(match.group(1)) * 1_000_000_000_000
    return None


def _os_disk_capacities(drives: list[Any]) -> tuple[int, ...]:
    """
    Capacities that identify a server's OS disks: the single smallest present.

    All-one-size means no OS disks at all, not all of them.

    Args:
        drives (list[Any]): The server's `StorageDrive`s.

    Returns:
        tuple[int, ...]: The single smallest capacity, or empty when the
            drives carry no capacity or are all one size.
    """
    capacities = {d.capacity_bytes for d in drives if d.capacity_bytes}
    if len(capacities) < 2:
        return ()
    return (min(capacities),)


def extract_facts(server: Server) -> dict[str, Any]:
    """
    Flatten a `Server` into the dotted-key facts dict the metric registry resolves against.

    Args:
        server (Server): The server to extract facts from.

    Returns:
        dict[str, Any]: A flat mapping of dotted metric-name-shaped keys
            (`"cpu.socket_count"`, `"storage.failed_drive_count"`, ...) to
            their current values.
    """
    drive_healths = [d.health for d in server.hardware.storage.drives if d.health is not None]
    link_states = [i.link_state.value for i in server.network.interfaces]
    psu_healths = [p.health for p in server.hardware.power.psus if p.health is not None]
    dimms = server.hardware.memory.modules
    drives = server.hardware.storage.drives
    os_capacities = _os_disk_capacities(drives)
    os_disks = [d for d in drives if d.capacity_bytes in os_capacities]
    data_disks = [d for d in drives if d.capacity_bytes not in os_capacities]
    gpus = server.hardware.gpus
    gpu_healths = [g.health for g in gpus if g.health is not None]
    uncorrectable = [
        g.uncorrectable_error_count for g in gpus if g.uncorrectable_error_count is not None
    ]
    name_capacity_bytes = _name_capacity_bytes(server.name)

    return {
        "cpu.socket_count": server.hardware.cpu.sockets,
        "memory.total_bytes": server.hardware.memory.total_bytes,
        "storage.drive_count": len(server.hardware.storage.drives),
        "storage.drive_healths": drive_healths,
        "storage.failed_drive_count": sum(
            1 for h in drive_healths if h == HealthSeverity.CRITICAL.value
        ),
        "storage.warning_drive_count": sum(
            1 for h in drive_healths if h == HealthSeverity.WARNING.value
        ),
        "storage.total_bytes": server.hardware.storage.total_bytes,
        "storage.os_disk_count": len(os_disks),
        "storage.os_bad_disk_count": sum(1 for d in os_disks if d.health in _NOT_GOOD),
        "storage.data_disk_count": len(data_disks),
        "storage.data_bad_disk_count": sum(1 for d in data_disks if d.health in _NOT_GOOD),
        **{
            fact: _has_name_token(server.name, token)
            for fact, token in _STORAGE_NAME_TOKENS.items()
        },
        "storage.name_capacity_bytes": name_capacity_bytes,
        "storage.capacity_deviation_bytes": (
            abs(server.hardware.storage.total_bytes - name_capacity_bytes)
            if name_capacity_bytes is not None
            else None
        ),
        "cpu.has_data": server.hardware.cpu.sockets > 0,
        "memory.has_data": server.hardware.memory.total_bytes > 0 or bool(dimms),
        "storage.has_data": bool(drives) or server.hardware.storage.total_bytes > 0,
        "network.has_data": bool(link_states),
        "connectivity.has_data": any(
            (
                server.connectivity.facts.fabric_paths_total,
                server.connectivity.facts.fabric_paths_up,
                server.connectivity.facts.fabric_paths_down,
            )
        ),
        "power.has_data": bool(server.hardware.power.psus),
        "gpu.has_data": bool(gpus),
        "memory.dimm_count": len(dimms),
        "memory.degraded_dimm_count": sum(1 for d in dimms if d.health in _NOT_GOOD),
        "network.interface_link_states": link_states,
        "network.interface_count": len(link_states),
        # The denominator every link policy uses, not `interface_count` (ADR-0027).
        "network.links_known_count": sum(1 for s in link_states if s != "UNKNOWN"),
        "network.links_up_count": sum(1 for s in link_states if s == "UP"),
        "connectivity.fabric_paths_total": server.connectivity.facts.fabric_paths_total,
        "connectivity.fabric_paths_up": server.connectivity.facts.fabric_paths_up,
        "connectivity.fabric_paths_down": server.connectivity.facts.fabric_paths_down,
        "power.psu_count": len(server.hardware.power.psus),
        "power.failed_psu_count": sum(1 for h in psu_healths if h == "DOWN"),
        "gpu.count": len(gpus),
        "gpu.failed_count": sum(1 for h in gpu_healths if h in _FAILED),
        "gpu.uncorrectable_error_count": sum(uncorrectable),
    }
