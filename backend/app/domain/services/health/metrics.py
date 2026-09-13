"""The health metric registry.

Declared in code, not the database: every metric a health policy can
reference must be a known, typed entry here, so an operator/metric
mismatch (`COUNT_GT` against a scalar, `IN` with a non-list value) is
rejected when a policy is *saved*, not discovered mid-evaluation against
10,000 servers. Extensible per module — a future vendor package registers
its own metrics by calling `register()` at import time, the same way core
metrics are registered below; `register()` raises on a duplicate name so a
naming collision between two provider packages is a loud startup failure,
not a silent shadow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MetricType(StrEnum):
    """The value shapes a metric can carry, used to validate operator compatibility."""

    INT = "INT"
    FLOAT = "FLOAT"
    STRING = "STRING"
    BOOL = "BOOL"
    ENUM = "ENUM"
    LIST_STRING = "LIST_STRING"
    LIST_INT = "LIST_INT"


@dataclass(frozen=True, slots=True)
class MetricDef:
    """One registered metric: its name, type, and how to resolve it from a facts dict."""

    name: str
    type: MetricType
    category: str
    description: str
    resolver: Callable[[dict[str, Any]], Any]
    enum_values: tuple[str, ...] | None = None
    provider: str = "core"


class MetricRegistry:
    """
    The known health metrics, keyed by name.

    Not a singleton: built once at startup and passed explicitly, so a
    test can build a smaller one without monkeypatching.
    """

    def __init__(self) -> None:
        """Start with an empty registry."""
        self._metrics: dict[str, MetricDef] = {}

    def register(self, metric: MetricDef) -> None:
        """
        Add a metric definition to the registry.

        Args:
            metric (MetricDef): The metric to register.

        Raises:
            ValueError: If a metric with the same name is already registered.
        """
        if metric.name in self._metrics:
            raise ValueError(f"metric {metric.name!r} is already registered")
        self._metrics[metric.name] = metric

    def get(self, name: str) -> MetricDef | None:
        """
        Look up a registered metric by name.

        Args:
            name (str): The metric name.

        Returns:
            MetricDef | None: The matching definition, or `None` if unregistered.
        """
        return self._metrics.get(name)

    def __contains__(self, name: str) -> bool:
        """Whether `name` is a registered metric."""
        return name in self._metrics

    def all(self) -> list[MetricDef]:
        """
        List every registered metric.

        Returns:
            list[MetricDef]: All registered metrics, sorted by name.
        """
        return sorted(self._metrics.values(), key=lambda m: m.name)


# Fact resolvers read `extract_facts`' flat dict and never raise for a
# missing key — they return a type-appropriate empty/zero value.


def _get(facts: dict[str, Any], key: str, default: Any) -> Any:
    return facts.get(key, default)


def build_default_registry() -> MetricRegistry:
    """
    Build the registry of core metrics every deployment ships with.

    Returns:
        MetricRegistry: A registry with the cpu/memory/storage/network/
            connectivity/power core metrics registered.
    """
    registry = MetricRegistry()

    registry.register(
        MetricDef(
            name="cpu.socket_count",
            type=MetricType.INT,
            category="cpu",
            description="Number of populated CPU sockets",
            resolver=lambda f: _get(f, "cpu.socket_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="memory.total_bytes",
            type=MetricType.INT,
            category="memory",
            description="Total installed memory in bytes",
            resolver=lambda f: _get(f, "memory.total_bytes", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.drive_count",
            type=MetricType.INT,
            category="storage",
            description="Number of storage drives reported",
            resolver=lambda f: _get(f, "storage.drive_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.drive_healths",
            type=MetricType.LIST_STRING,
            category="storage",
            description="Health string reported per drive",
            resolver=lambda f: _get(f, "storage.drive_healths", []),
        )
    )
    registry.register(
        MetricDef(
            name="storage.failed_drive_count",
            type=MetricType.INT,
            category="storage",
            description="Count of drives with health == CRITICAL",
            resolver=lambda f: _get(f, "storage.failed_drive_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="network.interface_link_states",
            type=MetricType.LIST_STRING,
            category="network",
            description="Link state reported per network interface",
            resolver=lambda f: _get(f, "network.interface_link_states", []),
        )
    )
    registry.register(
        MetricDef(
            name="connectivity.fabric_paths_total",
            type=MetricType.INT,
            category="connectivity",
            description="Total fabric attachments reported",
            resolver=lambda f: _get(f, "connectivity.fabric_paths_total", 0),
        )
    )
    registry.register(
        MetricDef(
            name="connectivity.fabric_paths_up",
            type=MetricType.INT,
            category="connectivity",
            description="Fabric attachments with oper_state == UP",
            resolver=lambda f: _get(f, "connectivity.fabric_paths_up", 0),
        )
    )
    registry.register(
        MetricDef(
            name="connectivity.fabric_paths_down",
            type=MetricType.INT,
            category="connectivity",
            description="Fabric attachments with oper_state == DOWN",
            resolver=lambda f: _get(f, "connectivity.fabric_paths_down", 0),
        )
    )
    registry.register(
        MetricDef(
            name="power.psu_count",
            type=MetricType.INT,
            category="power",
            description="Number of power supplies reported",
            resolver=lambda f: _get(f, "power.psu_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="power.failed_psu_count",
            type=MetricType.INT,
            category="power",
            description="Count of PSUs with health == DOWN",
            resolver=lambda f: _get(f, "power.failed_psu_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.warning_drive_count",
            type=MetricType.INT,
            category="storage",
            description="Count of drives with health == WARNING",
            resolver=lambda f: _get(f, "storage.warning_drive_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.total_bytes",
            type=MetricType.INT,
            category="storage",
            description="Total storage capacity across all drives, in bytes",
            resolver=lambda f: _get(f, "storage.total_bytes", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.os_disk_count",
            type=MetricType.INT,
            category="storage",
            description="Drives identified as OS disks (the smallest capacity present)",
            resolver=lambda f: _get(f, "storage.os_disk_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.os_bad_disk_count",
            type=MetricType.INT,
            category="storage",
            description="OS disks reporting WARNING or CRITICAL health",
            resolver=lambda f: _get(f, "storage.os_bad_disk_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.data_disk_count",
            type=MetricType.INT,
            category="storage",
            description="Drives that are not OS disks",
            resolver=lambda f: _get(f, "storage.data_disk_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.data_bad_disk_count",
            type=MetricType.INT,
            category="storage",
            description="Non-OS drives reporting WARNING or CRITICAL health",
            resolver=lambda f: _get(f, "storage.data_bad_disk_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="server.name_has_10tb",
            type=MetricType.BOOL,
            category="storage",
            description="Whether the server's name carries the 10TB token",
            resolver=lambda f: _get(f, "server.name_has_10tb", False),
        )
    )
    registry.register(
        MetricDef(
            name="server.name_has_5tb",
            type=MetricType.BOOL,
            category="storage",
            description="Whether the server's name carries the 5TB token",
            resolver=lambda f: _get(f, "server.name_has_5tb", False),
        )
    )
    registry.register(
        MetricDef(
            name="memory.dimm_count",
            type=MetricType.INT,
            category="memory",
            description="Number of fitted DIMMs reported",
            resolver=lambda f: _get(f, "memory.dimm_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="memory.degraded_dimm_count",
            type=MetricType.INT,
            category="memory",
            description="DIMMs reporting WARNING or CRITICAL health",
            resolver=lambda f: _get(f, "memory.degraded_dimm_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="network.interface_count",
            type=MetricType.INT,
            category="network",
            description="Number of network interfaces reported",
            resolver=lambda f: _get(f, "network.interface_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="network.links_up_count",
            type=MetricType.INT,
            category="network",
            description="Count of interfaces with link_state == UP",
            resolver=lambda f: _get(f, "network.links_up_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="network.links_known_count",
            type=MetricType.INT,
            category="network",
            description="Interfaces whose link state was actually readable (not UNKNOWN)",
            resolver=lambda f: _get(f, "network.links_known_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="gpu.count",
            type=MetricType.INT,
            category="gpu",
            description="Number of GPUs reported",
            resolver=lambda f: _get(f, "gpu.count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="gpu.failed_count",
            type=MetricType.INT,
            category="gpu",
            description="Count of GPUs reporting CRITICAL or DOWN health",
            resolver=lambda f: _get(f, "gpu.failed_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="gpu.uncorrectable_error_count",
            type=MetricType.INT,
            category="gpu",
            description="Uncorrectable ECC errors summed across the server's GPUs",
            resolver=lambda f: _get(f, "gpu.uncorrectable_error_count", 0),
        )
    )
    return registry


# Enforced at policy write time (`validate_condition`), not at evaluation.
OPERATOR_ALLOWED_TYPES: dict[str, frozenset[MetricType]] = {
    "EQ": frozenset(
        {MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.BOOL, MetricType.ENUM}
    ),
    "NE": frozenset(
        {MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.BOOL, MetricType.ENUM}
    ),
    "GT": frozenset({MetricType.INT, MetricType.FLOAT}),
    "GTE": frozenset({MetricType.INT, MetricType.FLOAT}),
    "LT": frozenset({MetricType.INT, MetricType.FLOAT}),
    "LTE": frozenset({MetricType.INT, MetricType.FLOAT}),
    "IN": frozenset({MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.ENUM}),
    "NOT_IN": frozenset({MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.ENUM}),
    "EXISTS": frozenset(MetricType),
    "NOT_EXISTS": frozenset(MetricType),
    "ANY": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "ALL": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_EQ": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_GT": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_GTE": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_LT": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_LTE": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
}
