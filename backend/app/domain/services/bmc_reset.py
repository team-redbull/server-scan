"""When a Dell server's BMC is worth restarting (ADR-0050)."""

from __future__ import annotations

from typing import Literal

from app.domain.enums import ManagerType, Vendor
from app.domain.models.server import Server
from app.domain.services.health.facts import extract_facts

ResetTrigger = Literal["disks", "nics", "both"]


def reset_trigger(server: Server) -> ResetTrigger | None:
    """
    Name what is unreadable on a reachable `OPENMANAGE` Dell server: all disks, all NICs, or both.

    Args:
        server (Server): The server as just persisted.

    Returns:
        ResetTrigger | None: Which side is all-UNKNOWN, `None` when neither (or not eligible).
    """
    if (
        server.identity.vendor is not Vendor.DELL
        or server.source_provider != ManagerType.OPENMANAGE.value
        or not server.reachable
    ):
        return None
    facts = extract_facts(server)
    disks = bool(facts.get("storage.all_unknown"))
    nics = bool(facts.get("network.all_unknown"))
    if disks and nics:
        return "both"
    if disks:
        return "disks"
    return "nics" if nics else None
