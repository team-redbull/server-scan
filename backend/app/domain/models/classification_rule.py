"""The `classification_rules` collection.

Distinct from `app.domain.models.classification.Classification` (the small
*result* embedded on a `Server` document, already built in slice 1) — this
is the rule that produces that result. Kept in a separate module rather
than added to `classification.py` because a rule and a classification
result have entirely different lifecycles: rules are authored/edited by
operators, results are computed and overwritten by the engine.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import InstallationType, ManagerType, Vendor

# Enforced at write time by the classification service, not here — see
# docs/architecture.md, "Classification engine".
PRIORITY_BANDS: dict[str, tuple[int, int]] = {
    "SITE_CUSTOM": (500, 599),
    "MANAGER_CUSTOM": (400, 499),
    "VENDOR_CUSTOM": (300, 399),
    "GLOBAL_CUSTOM": (200, 299),
    "SYSTEM_DEFAULT": (100, 199),
}

CLASSIFIABLE_FIELDS = frozenset({"name", "hostname", "serial", "model", "site_id"})


class RuleScope(BaseModel):
    """The vendor/manager-type/site dimensions a classification rule is scoped to."""

    vendor: Vendor | None = None
    manager_type: ManagerType | None = None
    site_id: str | None = None

    def specificity(self) -> int:
        """
        Score how specific this scope is, for resolving overlapping rules.

        Powers of two, so a more-specific scope strictly outranks a less
        specific one however many dimensions are set.

        Returns:
            int: A specificity score; higher means more specific.
        """
        return (
            (4 if self.site_id is not None else 0)
            + (2 if self.manager_type is not None else 0)
            + (1 if self.vendor is not None else 0)
        )

    def matches(
        self, *, vendor: Vendor, manager_type: ManagerType | None, site_id: str | None
    ) -> bool:
        """
        Check whether a server's dimensions satisfy this scope.

        Args:
            vendor (Vendor): The server's vendor.
            manager_type (ManagerType | None): The server's manager type, if any.
            site_id (str | None): The server's site id, if any.

        Returns:
            bool: True if every dimension set on this scope matches.
        """
        if self.vendor is not None and self.vendor != vendor:
            return False
        if self.manager_type is not None and self.manager_type != manager_type:
            return False
        return not (self.site_id is not None and self.site_id != site_id)


class RuleFlags(BaseModel):
    """Regex evaluation flags applied when matching a classification rule's pattern."""

    ignore_case: bool = True
    multiline: bool = False
    dotall: bool = False


class RuleStats(BaseModel):
    """Runtime statistics tracked for a classification rule's matches."""

    last_matched_at: datetime | None = None
    timeout_count: int = 0
    quarantined: bool = False


class ClassificationRule(BaseModel):
    """One document in the `classification_rules` collection."""

    id: str = Field(alias="_id")
    name: str
    description: str = ""
    enabled: bool = True
    system: bool = False

    installation_type: InstallationType
    scope: RuleScope = Field(default_factory=RuleScope)
    field: str
    pattern: str
    flags: RuleFlags = Field(default_factory=RuleFlags)

    source: str
    priority: int
    order: int = 0

    stats: RuleStats = Field(default_factory=RuleStats)

    revision: int = 1
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None

    model_config = {"populate_by_name": True}
