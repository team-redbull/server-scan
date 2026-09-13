"""Response models for `GET /api/v1/sites`.

Mutable (not frozen) on purpose: `app.api.v1.sites._pivot` builds these
incrementally as it folds the aggregation buckets, which is the one place
they are constructed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VendorCount(BaseModel):
    """How many servers of one vendor a breakdown counts."""

    vendor: str
    count: int


class Breakdown(BaseModel):
    """The counts one slice of the fleet reports.

    Shared by a whole site and by each slice within it, so the UI renders
    both with one component.
    """

    total: int = 0

    # A list, so the UI renders vendors in `Vendor`'s declaration order.
    by_vendor: list[VendorCount] = Field(default_factory=list)

    # Every severity is present, zeroes included; the UI never null-checks.
    by_health: dict[str, int] = Field(default_factory=dict)

    in_maintenance: int = 0


class SiteStats(Breakdown):
    """One site's fleet-wide breakdown, sliced further by installation type."""

    site_id: str
    name: str

    by_installation_type: dict[str, Breakdown] = Field(default_factory=dict)

    by_openshift_state: dict[str, Breakdown] = Field(default_factory=dict)


class FleetSummary(Breakdown):
    """Every site summed together, sliced further by installation type.

    Folded server-side in `sites._pivot`, not summed from `items` by the
    client, so every consumer gets the identical fleet-wide number.
    """

    by_installation_type: dict[str, Breakdown] = Field(default_factory=dict)
    by_openshift_state: dict[str, Breakdown] = Field(default_factory=dict)


class SiteStatsListResponse(BaseModel):
    """Every configured site's statistics, plus "Unassigned", plus the fleet-wide summary."""

    items: list[SiteStats]
    fleet: FleetSummary
