"""`GET /api/v1/sites` — the fixed site list and per-site statistics.

Sites are a closed set loaded from `INVENTORY_SITES` (see ADR-0018),
not rows a user creates, so this endpoint always returns every one in a
stable order whether or not any server currently reports one. A site with
zero servers renders as an empty site, never as a missing card — "site
four has nothing in it" and "site four does not exist" are different
facts and the UI should be able to tell them apart.

The `unassigned` bucket at the end counts servers whose name carries no
site token (see `app.domain.value_objects.site`). It is deliberately
surfaced rather than hidden: a growing unassigned count is how a naming
drift becomes visible.

Each site also reports the same counts sliced by installation type, which
is what lets the landing page show fleet-wide UPI and hosted-cluster
cards without a second query.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends

from app.api.v1.sites_schemas import (
    Breakdown,
    FleetSummary,
    SiteStats,
    SiteStatsListResponse,
    VendorCount,
)
from app.config import Settings, get_settings
from app.dependencies import get_mongo_holder, get_redis_holder
from app.domain.enums import HealthSeverity, InstallationType, OpenShiftState, Vendor
from app.domain.ports.repository import SiteBreakdownRow
from app.domain.value_objects.site import UNASSIGNED_SITE_ID, SiteCatalog, site_catalog
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.cache import CacheClient
from app.infrastructure.redis.client import RedisClientHolder

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sites"])

# A short TTL, not invalidation — docs/architecture.md, "caching".
_STATS_TTL_SECONDS = 30
# `:4:` is a schema version; bump it with every change to the response shape.
_STATS_CACHE_KEY = "si:4:sites:stats"

_VENDOR_ORDER: tuple[str, ...] = tuple(v.value for v in Vendor)
_HEALTH_ORDER: tuple[str, ...] = tuple(s.value for s in HealthSeverity)
_INSTALLATION_ORDER: tuple[str, ...] = tuple(t.value for t in InstallationType)
_OPENSHIFT_ORDER: tuple[str, ...] = tuple(s.value for s in OpenShiftState)

_VENDOR_INDEX = {vendor: position for position, vendor in enumerate(_VENDOR_ORDER)}


async def _server_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MongoServerRepository:
    """
    Build the server repository for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.
        settings (Settings): Supplies the cursor-signing secret.

    Returns:
        MongoServerRepository: A repository bound to that client.
    """
    return MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)


async def _cache(
    redis: Annotated[RedisClientHolder, Depends(get_redis_holder)],
) -> CacheClient:
    """
    Build the cache-aside client for one request.

    Args:
        redis (RedisClientHolder): The shared Redis client holder.

    Returns:
        CacheClient: A cache client bound to that connection.
    """
    return CacheClient(redis)


def _empty_breakdown() -> Breakdown:
    """Build a zeroed breakdown with every vendor and severity present.

    Returns:
        Breakdown: All counts at zero, all keys populated.
    """
    return Breakdown(
        by_vendor=[VendorCount(vendor=vendor, count=0) for vendor in _VENDOR_ORDER],
        by_health=dict.fromkeys(_HEALTH_ORDER, 0),
    )


def _empty_stats(site_id: str, *, name: str) -> SiteStats:
    """Build a zeroed record for one site.

    Args:
        site_id (str): The site code, or `UNASSIGNED_SITE_ID`.
        name (str): The site's display name.

    Returns:
        SiteStats: All counts at zero, all keys populated.
    """
    return SiteStats(
        site_id=site_id,
        name=name,
        by_vendor=[VendorCount(vendor=vendor, count=0) for vendor in _VENDOR_ORDER],
        by_health=dict.fromkeys(_HEALTH_ORDER, 0),
        by_installation_type={
            installation: _empty_breakdown() for installation in _INSTALLATION_ORDER
        },
        by_openshift_state={state: _empty_breakdown() for state in _OPENSHIFT_ORDER},
    )


def _accumulate(entry: Breakdown, row: SiteBreakdownRow) -> None:
    """Add one aggregation bucket into a breakdown, in place.

    An unknown enum value lands in the catch-all bucket rather than being
    dropped, so slice totals always add up to the fleet size.

    Args:
        entry (Breakdown): The breakdown to add into.
        row (SiteBreakdownRow): The bucket to add.
    """
    entry.total += row.count
    if row.maintenance:
        entry.in_maintenance += row.count

    if row.health in entry.by_health:
        entry.by_health[row.health] += row.count
    else:
        entry.by_health[HealthSeverity.UNKNOWN.value] += row.count

    position = _VENDOR_INDEX.get(row.vendor or "")
    if position is not None:
        entry.by_vendor[position].count += row.count


def _fallback_state(entry: SiteStats | FleetSummary) -> Breakdown:
    """The slice an unrecognized OpenShift state counts under.

    Args:
        entry (SiteStats | FleetSummary): The record being folded into.

    Returns:
        Breakdown: The `AVAILABLE` slice.
    """
    return entry.by_openshift_state[OpenShiftState.AVAILABLE.value]


def _pivot(
    rows: list[SiteBreakdownRow], sites: SiteCatalog
) -> tuple[list[SiteStats], FleetSummary]:
    """Fold the flat `$group` buckets into one record per site, and one fleet-wide summary.

    Every configured site is seeded first, so the response shape never
    depends on the database; docs/architecture.md, "caching", has the rest.

    Args:
        rows (list[SiteBreakdownRow]): The aggregation's flat buckets.
        sites (SiteCatalog): The configured sites.

    Returns:
        tuple[list[SiteStats], FleetSummary]: One record per site plus
            "Unassigned", and the totals across all of them.
    """
    stats: dict[str, SiteStats] = {
        definition.code: _empty_stats(definition.code, name=definition.name)
        for definition in sites.definitions
    }
    stats[UNASSIGNED_SITE_ID] = _empty_stats(UNASSIGNED_SITE_ID, name="Unassigned")

    fleet = FleetSummary(
        by_vendor=[VendorCount(vendor=vendor, count=0) for vendor in _VENDOR_ORDER],
        by_health=dict.fromkeys(_HEALTH_ORDER, 0),
        by_installation_type={
            installation: _empty_breakdown() for installation in _INSTALLATION_ORDER
        },
        by_openshift_state={state: _empty_breakdown() for state in _OPENSHIFT_ORDER},
    )

    for row in rows:
        site_id = row.site_id or UNASSIGNED_SITE_ID
        entry = stats.get(site_id)
        if entry is None:
            # A site renamed away since the document was written counts as
            # unassigned, so totals still add up.
            entry = stats[UNASSIGNED_SITE_ID]

        _accumulate(entry, row)
        _accumulate(fleet, row)

        installation = entry.by_installation_type.get(row.installation_type or "")
        if installation is None:
            installation = entry.by_installation_type[InstallationType.UNCLASSIFIED.value]
        _accumulate(installation, row)

        fleet_installation = fleet.by_installation_type.get(row.installation_type or "")
        if fleet_installation is None:
            fleet_installation = fleet.by_installation_type[InstallationType.UNCLASSIFIED.value]
        _accumulate(fleet_installation, row)

        # A pre-ADR-0024 state counts as AVAILABLE, so totals still add up.
        state = row.openshift_state or ""
        _accumulate(entry.by_openshift_state.get(state) or _fallback_state(entry), row)
        _accumulate(fleet.by_openshift_state.get(state) or _fallback_state(fleet), row)

    return list(stats.values()), fleet


@router.get("/sites", response_model=SiteStatsListResponse)
async def list_sites(
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SiteStatsListResponse:
    """
    List every configured site with its per-site and fleet-wide statistics.

    Args:
        repo (MongoServerRepository): The server repository.
        cache (CacheClient): Cache-aside for the aggregation.
        settings (Settings): Supplies the configured site catalog.

    Returns:
        SiteStatsListResponse: One record per configured site, plus
            "Unassigned", plus the fleet-wide summary across all of them.
    """
    cached = await cache.get(_STATS_CACHE_KEY)
    if cached is not None:
        return SiteStatsListResponse.model_validate(cached)

    items, fleet = _pivot(await repo.site_breakdown(), site_catalog(settings.sites))
    response = SiteStatsListResponse(items=items, fleet=fleet)
    await cache.set(
        _STATS_CACHE_KEY, response.model_dump(mode="json"), ttl_seconds=_STATS_TTL_SECONDS
    )
    return response
