"""The `health_policies` collection.

`policy_key` is the mechanism that solves the override problem the
platform spec poses: a site-scoped policy must be able to *replace* a
global default (not just add another alert alongside it), while unrelated
policies keep firing independently. Policies sharing a `policy_key` form a
family; within a family, only the highest-precedence member is evaluated
(see `app.domain.services.health.resolution`) — different keys are fully
independent and all evaluate. `policy_key` defaults to the policy's own id
when not explicitly shared, which is what makes "just add a policy" the
common case and "this policy replaces that one" an explicit, visible
choice (the same `policy_key` value) rather implicit merge logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import HealthSeverity
from app.domain.models.classification_rule import PRIORITY_BANDS
from app.domain.services.health.conditions import Condition

# A policy in SUPPRESS mode contributes no evaluation even if its
# condition would match — this is how a scope disables an inherited
# default without needing to also override its condition/severity.
PolicyMode = str  # "EVALUATE" | "SUPPRESS"


class PolicyScope(BaseModel):
    """The site/vendor/manager-type dimensions a health policy is scoped to."""

    site_id: str | None = None
    vendor: str | None = None
    manager_types: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy_manager_type(cls, data: Any) -> Any:
        """
        Accept the pre-ADR-0030 scalar `manager_type` key a stored document may carry.

        Args:
            data (Any): The raw input, a dict for a stored document.

        Returns:
            Any: The input with `manager_type` folded into `manager_types`.
        """
        if isinstance(data, dict) and "manager_type" in data:
            data = dict(data)
            legacy = data.pop("manager_type")
            data.setdefault("manager_types", [] if legacy is None else [legacy])
        return data

    def specificity(self) -> int:
        """
        Score how specific this scope is, for resolving `policy_key` families.

        Returns:
            int: A specificity score; higher means more specific.
        """
        return (
            (4 if self.site_id is not None else 0)
            + (2 if self.manager_types else 0)
            + (1 if self.vendor is not None else 0)
        )

    def matches(self, *, vendor: str, manager_type: str | None, site_id: str | None) -> bool:
        """
        Check whether a server's dimensions satisfy this scope.

        Args:
            vendor (str): The server's vendor.
            manager_type (str | None): The server's manager type, if any.
            site_id (str | None): The server's site id, if any.

        Returns:
            bool: True if every dimension set on this scope matches.
        """
        if self.vendor is not None and self.vendor != vendor:
            return False
        if self.manager_types and manager_type not in self.manager_types:
            return False
        return not (self.site_id is not None and self.site_id != site_id)


class EvidenceField(BaseModel):
    """One metric a fired policy cites as evidence, keyed for the message template."""

    key: str
    metric: str


class PolicyStats(BaseModel):
    """Runtime statistics tracked for a health policy's evaluations."""

    fire_count: int = 0
    last_fired_at: datetime | None = None
    error_count: int = 0
    quarantined: bool = False


class HealthPolicy(BaseModel):
    """One document in the `health_policies` collection."""

    id: str = Field(alias="_id")
    name: str
    description: str = ""
    enabled: bool = True
    system: bool = False

    policy_key: str
    mode: str = "EVALUATE"  # EVALUATE | SUPPRESS
    category: str  # cpu | memory | storage | network | connectivity | power | gpu
    severity: HealthSeverity
    condition: Condition
    evidence: list[EvidenceField] = Field(default_factory=list)
    message_template: str

    scope: PolicyScope = Field(default_factory=PolicyScope)
    source: str  # SITE_CUSTOM | MANAGER_CUSTOM | VENDOR_CUSTOM | GLOBAL_CUSTOM | SYSTEM_DEFAULT
    priority: int
    order: int = 0

    stats: PolicyStats = Field(default_factory=PolicyStats)

    revision: int = 1
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _priority_within_band(self) -> HealthPolicy:
        band = PRIORITY_BANDS.get(self.source)
        if band is not None and not (band[0] <= self.priority <= band[1]):
            raise ValueError(f"priority {self.priority} is outside the {self.source} band {band}")
        return self

    @model_validator(mode="after")
    def _mode_is_known(self) -> HealthPolicy:
        if self.mode not in ("EVALUATE", "SUPPRESS"):
            raise ValueError(f"unknown mode {self.mode!r}")
        return self
