"""The platform's closed-set domain enumerations. See `core.py`."""

from app.domain.enums.core import (
    HEALTH_SEVERITY_RANK,
    HealthSeverity,
    InstallationType,
    LinkState,
    ManagerType,
    MediaType,
    OpenShiftState,
    ServerType,
    UnreachableReason,
    Vendor,
    server_type_of,
)

__all__ = [
    "HEALTH_SEVERITY_RANK",
    "HealthSeverity",
    "InstallationType",
    "LinkState",
    "ManagerType",
    "MediaType",
    "OpenShiftState",
    "ServerType",
    "UnreachableReason",
    "Vendor",
    "server_type_of",
]
