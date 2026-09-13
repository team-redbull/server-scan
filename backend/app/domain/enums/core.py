"""Core domain enumerations.

Plain `str, Enum` (not a bare string-constant class like `ErrorCode`)
because these values are embedded directly in Pydantic domain models and
benefit from Pydantic's enum validation/serialization — invalid values are
rejected at the model boundary rather than accepted as arbitrary strings.
`ErrorCode` stays a string-constant class because it is never a model
field, only ever compared against; an enum would add nothing there.
"""

from __future__ import annotations

from enum import StrEnum


class Vendor(StrEnum):
    """
    The vendors this platform ingests from; no `UNKNOWN`, by design (docs/adr/0011).

    `STANDALONE` is a manufacturer this platform does not model, not
    "collected without a manager" — see docs/adr/0016.
    """

    DELL = "dell"
    CISCO = "cisco"
    HP = "hp"
    STANDALONE = "standalone"


class ManagerType(StrEnum):
    """
    How this platform reaches a server.

    `REDFISH_STANDALONE` names no manager: one BMC at a time, for machines
    no aggregator owns (docs/adr/0016).
    """

    OPENMANAGE = "OPENMANAGE"
    UCS_MANAGER = "UCS_MANAGER"
    UCS_CENTRAL = "UCS_CENTRAL"
    INTERSIGHT = "INTERSIGHT"
    ONEVIEW = "ONEVIEW"
    REDFISH_STANDALONE = "REDFISH_STANDALONE"


class InstallationType(StrEnum):
    """A server's role, as a regex verdict on its hostname (classification)."""

    HOSTED_CLUSTER = "HOSTED_CLUSTER"
    MCE = "MCE"
    UPI = "UPI"
    UNCLASSIFIED = "UNCLASSIFIED"


class OpenShiftState(StrEnum):
    """
    Whether a server is in use, as a cluster or MCE reports it.

    Parallel to `InstallationType` (a regex verdict on the name) and
    deliberately not the same thing — see docs/adr/0024, Decision 3.
    """

    AVAILABLE = "AVAILABLE"
    """No cluster claims this server. The default, and the only state reached by absence."""

    INSTALLED = "INSTALLED"
    """In use: a node in a cluster, or an Agent bound to a hosted cluster."""

    INSTALLED_TO_INVENTORY = "INSTALLED_TO_INVENTORY"
    """Registered to an MCE and bound to no cluster — spare capacity it can deploy."""


class HealthSeverity(StrEnum):
    """
    A server's overall or per-category health, from best to worst.

    Every aggregation sorts by `HEALTH_SEVERITY_RANK`. `MAJOR` is "redundancy
    gone, still serving"; `INFO` was retired 2026-09-13 — docs/architecture.md.
    """

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


# Explicit, not declaration order: a StrEnum reorder would silently swap
# CRITICAL and MAJOR in every worst-of aggregation.
HEALTH_SEVERITY_RANK: dict[HealthSeverity, int] = {
    HealthSeverity.UNKNOWN: 0,
    HealthSeverity.HEALTHY: 1,
    HealthSeverity.WARNING: 2,
    HealthSeverity.MAJOR: 3,
    HealthSeverity.CRITICAL: 4,
}


class LinkState(StrEnum):
    """A network interface's reported link state."""

    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


class MediaType(StrEnum):
    """A storage drive's reported media type."""

    HDD = "HDD"
    SSD = "SSD"
    NVME = "NVME"
    UNKNOWN = "UNKNOWN"
