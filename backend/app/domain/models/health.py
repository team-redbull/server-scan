"""Health rollup, embedded on `Server`.

Like `classification.py`, this declares the shape the health *engine*
(slice 3) writes to — evaluation/policy resolution logic lives there, not
here. A server with no policies evaluated yet is `UNKNOWN` in every
category, which is the correct reading of "no policy has said anything
about this yet", not a claim that the server is unhealthy.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.domain.enums import HealthSeverity

# `INFO`, retired 2026-09-13: a positive verdict with no shipped policy behind it.
_RETIRED_SEVERITIES: dict[str, HealthSeverity] = {"INFO": HealthSeverity.HEALTHY}

_SEVERITY_FIELDS = (
    "overall",
    "cpu",
    "memory",
    "storage",
    "network",
    "connectivity",
    "power",
    "gpu",
)


def decode_retired_severity(value: object) -> object:
    """
    Map a severity this enum no longer has onto its replacement.

    A narrowed persisted enum is a migration (ADR-0026); this keeps every
    document written before the narrowing loading.

    Args:
        value (object): The stored value, from MongoDB or a caller.

    Returns:
        object: `value` if the enum still has it, else its replacement.
    """
    if isinstance(value, str) and value in _RETIRED_SEVERITIES:
        return _RETIRED_SEVERITIES[value]
    return value


class CategoryHealth(BaseModel):
    """The evaluated severity for a single health category."""

    severity: HealthSeverity = HealthSeverity.UNKNOWN

    _decode = field_validator("severity", mode="before")(decode_retired_severity)


class Health(BaseModel):
    """The health rollup embedded on a `Server` document, one severity per category."""

    overall: HealthSeverity = HealthSeverity.UNKNOWN
    cpu: HealthSeverity = HealthSeverity.UNKNOWN
    memory: HealthSeverity = HealthSeverity.UNKNOWN
    storage: HealthSeverity = HealthSeverity.UNKNOWN
    network: HealthSeverity = HealthSeverity.UNKNOWN
    connectivity: HealthSeverity = HealthSeverity.UNKNOWN
    power: HealthSeverity = HealthSeverity.UNKNOWN
    gpu: HealthSeverity = HealthSeverity.UNKNOWN
    evaluated_at: datetime | None = None
    # The `policy_key`s that fired, so "what is wrong across the fleet" is
    # one aggregation (ADR-0029). Absent on documents written before it.
    active_policy_keys: list[str] = Field(default_factory=list)

    _decode = field_validator(*_SEVERITY_FIELDS, mode="before")(decode_retired_severity)
