"""Epoch mutations: a seed-stable ~6% of fake servers change health between seed runs.

Epoch 0 is the untouched generator output. Why the fault kinds were chosen
and how the health policies react: docs/architecture.md, "The fake provider's shape".
"""

from __future__ import annotations

import dataclasses
import zlib

from app.domain.ports.provider import ProviderNic, ProviderServer

_FLAPPER_PERCENT = 6
_KINDS = ("dimm", "drive", "psu", "network", "gpu")
_RECOVERED, _DEGRADED, _CRITICAL = 0, 1, 2
_DIMM_COUNT = 4


def _hash(*parts: object) -> int:
    """
    Stable integer from the given parts.

    Args:
        *parts (object): Values to hash together.

    Returns:
        int: A CRC32 of their `/`-joined text.
    """
    return zlib.crc32("/".join(str(p) for p in parts).encode())


def _applicable(server: ProviderServer, kind: str) -> bool:
    """
    Whether this server reported the component `kind` would mutate.

    Args:
        server (ProviderServer): The epoch-0 server.
        kind (str): One of `_KINDS`.

    Returns:
        bool: False for a component that was unread or absent.
    """
    if kind == "dimm":
        return server.memory_total_bytes is not None
    if kind == "drive":
        return bool(server.storage_drives)
    if kind == "psu":
        return bool(server.psus)
    if kind == "network":
        return bool(server.nics)
    return bool(server.gpus)


def _with_health(
    items: tuple[dict[str, object], ...], health: str, *, first: int | None
) -> tuple[dict[str, object], ...]:
    """
    Copy component dicts with `health` set on the first `first` of them (all if None).

    Args:
        items (tuple[dict[str, object], ...]): Drives or GPUs.
        health (str): The reading to apply.
        first (int | None): How many leading items to fault.

    Returns:
        tuple[dict[str, object], ...]: Every item healthy except the faulted ones.
    """
    n = len(items) if first is None else first
    return tuple(
        {**item, "health": health if i < n else "HEALTHY", "health_detail": None}
        for i, item in enumerate(items)
    )


def apply_epoch(server: ProviderServer, *, seed: int, index: int, epoch: int) -> ProviderServer:
    """
    Return `server` as it reads at `epoch`; epoch 0 and non-flappers come back unchanged.

    Args:
        server (ProviderServer): The epoch-0 server.
        seed (int): The fleet seed.
        index (int): The server's position in the fleet.
        epoch (int): Which epoch to render.

    Returns:
        ProviderServer: The server with its faulted component rewritten.
    """
    if epoch <= 0 or not server.reachable or _hash("flap", seed, index) % 100 >= _FLAPPER_PERCENT:
        return server
    start = _hash("kind", seed, index) % len(_KINDS)
    kind = next(
        (
            k
            for k in (_KINDS[(start + i) % len(_KINDS)] for i in range(len(_KINDS)))
            if _applicable(server, k)
        ),
        None,
    )
    if kind is None:
        return server
    level = _hash("level", seed, index, epoch) % 3
    if kind == "dimm":
        bad = {_RECOVERED: 0, _DEGRADED: 1, _CRITICAL: 2}[level]
        modules = tuple(
            {
                "slot": f"DIMM_{i}",
                "size_bytes": (server.memory_total_bytes or 0) // _DIMM_COUNT,
                "health": "CRITICAL" if i < bad else "HEALTHY",
            }
            for i in range(_DIMM_COUNT)
        )
        return dataclasses.replace(server, memory_modules=modules)
    if kind == "drive":
        drives = server.storage_drives or ()
        # Every drive failing includes the OS disk, which is what turns it CRITICAL.
        first = {_RECOVERED: 0, _DEGRADED: 1, _CRITICAL: None}[level]
        health = "HEALTHY" if level == _RECOVERED else "CRITICAL"
        return dataclasses.replace(server, storage_drives=_with_health(drives, health, first=first))
    if kind == "psu":
        down = {_RECOVERED: 0, _DEGRADED: 1, _CRITICAL: 2}[level]
        psus = tuple(
            {**p, "health": "DOWN" if i < down else "UP", "health_detail": None}
            for i, p in enumerate(server.psus or ())
        )
        return dataclasses.replace(server, psus=psus)
    if kind == "network":
        up = {_RECOVERED: len(server.nics), _DEGRADED: 1, _CRITICAL: 0}[level]
        nics = tuple(
            ProviderNic(
                name=n.name,
                mac=n.mac,
                speed_mbps=n.speed_mbps,
                link_state="UP" if i < up else "DOWN",
                location=n.location,
            )
            for i, n in enumerate(server.nics)
        )
        return dataclasses.replace(server, nics=nics)
    first = {_RECOVERED: 0, _DEGRADED: 1, _CRITICAL: None}[level]
    health = "HEALTHY" if level == _RECOVERED else "CRITICAL"
    return dataclasses.replace(server, gpus=_with_health(server.gpus or (), health, first=first))
