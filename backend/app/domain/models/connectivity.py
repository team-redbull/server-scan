"""Normalized fabric/connectivity model.

Not Cisco-specific despite Cisco UCS being the motivating case (fabric
interconnect A/B topology). `attachments` is a plain list of unbounded
length — nothing here assumes exactly two, because OneView, Intersight,
and non-fabric-interconnect topologies may report one, four, or zero.

This is the least-validated part of the schema: no code anywhere in the
existing UCS operator (`BareMetalHostUCS`) touches fabric interconnect
data at all (confirmed by grepping every commit in that repo's history —
only `lsServer`, `VnicEther`, `VnicIpV4PooledAddr`, `computeRackUnit`,
`mgmtInterface`, `adaptorUnit`, `adaptorHostEthIf` are ever queried). Until
a real UCS Manager collector exists, the fake data generator is the only
thing exercising this shape — treat it as the most likely part of the
schema to need revision once real UCS fabric data is seen.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ConnectivityAttachment(BaseModel):
    """One reported fabric attachment — a physical uplink or a vNIC carved out of one."""

    type: str = "UNKNOWN"
    provider: str | None = None
    fabric: str | None = None
    fabric_name: str | None = None
    fabric_id: str | None = None
    fabric_model: str | None = None
    fabric_serial: str | None = None
    server_interface: str | None = None
    server_port: str | None = None
    fabric_port: str | None = None
    admin_state: str = "UNKNOWN"
    oper_state: str = "UNKNOWN"
    speed_mbps: int | None = None
    interface_kind: str = "PHYSICAL"  # or "VNIC"; only PHYSICAL counts as a fabric path
    last_seen: datetime | None = None


class ConnectivityFacts(BaseModel):
    """
    Fabric-path scalars derived from `attachments` once at ingest and stored.

    `total != up + down` is deliberate: UNKNOWN/DEGRADED counts toward neither.
    """

    fabric_paths_total: int = 0
    fabric_paths_up: int = 0
    fabric_paths_down: int = 0
    fabrics_present: list[str] = Field(default_factory=list)


class Connectivity(BaseModel):
    """A server's fabric attachments plus the derived facts computed from them."""

    attachments: list[ConnectivityAttachment] = Field(default_factory=list)
    facts: ConnectivityFacts = Field(default_factory=ConnectivityFacts)


def compute_connectivity_facts(attachments: list[ConnectivityAttachment]) -> ConnectivityFacts:
    """
    Derive `ConnectivityFacts` from a list of attachments.

    Only `PHYSICAL` attachments count as paths — see docs/architecture.md,
    "The provider contract".

    Args:
        attachments (list[ConnectivityAttachment]): Every attachment
            reported for one server, of both kinds.

    Returns:
        ConnectivityFacts: The derived scalars health policies evaluate.
    """
    physical = [a for a in attachments if a.interface_kind == "PHYSICAL"]
    up = sum(1 for a in physical if a.oper_state == "UP")
    down = sum(1 for a in physical if a.oper_state == "DOWN")
    fabrics_present = sorted({a.fabric for a in physical if a.fabric is not None})
    return ConnectivityFacts(
        fabric_paths_total=len(physical),
        fabric_paths_up=up,
        fabric_paths_down=down,
        fabrics_present=fabrics_present,
    )
