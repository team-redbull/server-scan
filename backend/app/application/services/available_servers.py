"""`GET /servers/available`: Mongo-side candidate resolution plus a live recheck.

Two lookup modes share one pipeline: find candidates in Mongo (fast,
fleet-wide), then live-verify only the few servers about to be returned —
never the whole matching set. See
docs/adr/0032-available-server-lookup-api.md for the full design, the
health-tier fill order, and why a manager type with no credentials
configured on this process degrades to trusting the stored document
rather than failing the request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.application.services.ingest import IngestService
from app.domain.enums import HealthSeverity, ManagerType, OpenShiftState
from app.domain.models.server import Server
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.ports.provider import ServerIdentity, ServerInventoryProvider
from app.domain.ports.repository import ServerRepository
from app.domain.services.normalize import normalize_text
from app.domain.value_objects.capacity_aliases import CapacityAliasCatalog

# Best to worst; never CRITICAL, and UNKNOWN (never evaluated) is excluded too (ADR-0027).
SELECTABLE_TIERS: tuple[HealthSeverity, ...] = (
    HealthSeverity.HEALTHY,
    HealthSeverity.WARNING,
    HealthSeverity.MAJOR,
)

_SELECTABLE_VALUES = [tier.value for tier in SELECTABLE_TIERS]

# `health.overall` cannot carry this: UNKNOWN ranks BELOW HEALTHY and never
# raises a max(), so a server whose links were never read rolls up HEALTHY off
# its other categories — docs/adr/0032, 2026-09-26 update.
REQUIRED_NETWORK_HEALTH = HealthSeverity.HEALTHY

# Bounds worst-case cost when candidates keep failing their live recheck —
# ADR-0032, "candidate ranking".
_MAX_REPLACEMENT_ROUNDS_PER_TIER = 3


class AvailableServersNotFoundError(Exception):
    """No server could be returned at all; `reason` is the human-readable detail."""

    def __init__(self, reason: str) -> None:
        """
        Build the error.

        Args:
            reason (str): What made the result empty, for the route's
                RFC 9457 `detail`.
        """
        super().__init__(reason)
        self.reason = reason


class AmbiguousServerNameError(Exception):
    """`?name=` matched several documents; `server_ids` names them.

    Names are not unique — ADR-0032's 2026-09-25 update.
    """

    def __init__(self, name: str, server_ids: list[str]) -> None:
        """
        Build the error.

        Args:
            name (str): The requested name.
            server_ids (list[str]): Every matching server's id.
        """
        super().__init__(f"{name!r} matches {len(server_ids)} servers")
        self.name = name
        self.server_ids = server_ids


@dataclass(frozen=True, slots=True)
class AvailableServerResult:
    """One candidate this lookup is returning.

    Attributes:
        server (Server): The persisted, current server document.
        live_recheck_performed (bool): Whether `get_one()` actually ran —
            `False` means this candidate's health/state is whatever the
            last collection run saw (ADR-0032, Decision 5).
    """

    server: Server
    live_recheck_performed: bool


@dataclass(frozen=True, slots=True)
class AvailableServersOutcome:
    """The full result of one pattern-mode lookup."""

    items: list[AvailableServerResult]
    requested: int


def nic_mac_filters(min_nic_macs: int) -> dict[str, object]:
    """
    The Mongo clauses requiring `min_nic_macs` NIC MACs that were actually read.

    Both halves are load-bearing, and neither is implied by the health tiers —
    ADR-0032's 2026-09-25 update.

    Args:
        min_nic_macs (int): How many MACs the caller needs. `0` imposes
            nothing, which is the default.

    Returns:
        dict[str, object]: Clauses to merge into an assignability filter.
    """
    if min_nic_macs < 1:
        return {}
    return {
        # Dotted-index existence is Mongo's array-length-at-least test.
        f"identity.nic_macs.{min_nic_macs - 1}": {"$exists": True},
        "unread_fields": {"$ne": "identity.nic_macs"},
    }


def server_still_qualifies(
    server: Server,
    *,
    tiers: tuple[HealthSeverity, ...] = SELECTABLE_TIERS,
    min_nic_macs: int = 0,
) -> bool:
    """
    Whether a server's current state still makes it assignable.

    The same predicate both modes apply after a live recheck, and it must agree
    with the Mongo filters the draw used (ADR-0032).

    Args:
        server (Server): The server, as freshly rechecked (or, on a
            degraded recheck, as last stored).
        tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
        min_nic_macs (int): How many NIC MACs must have been read.

    Returns:
        bool: `True` when it is `AVAILABLE`, not in maintenance, reachable, at
            an acceptable health tier, carrying enough freshly-read MACs, and
            with its network category read and passing.
    """
    # Gated on the caller asking, so this admits exactly what the draw
    # returned — ADR-0032's 2026-09-26 update.
    if min_nic_macs >= 1 and (
        len(server.identity.nic_macs) < min_nic_macs or "identity.nic_macs" in server.unread_fields
    ):
        return False
    return (
        server.openshift.lifecycle_state == OpenShiftState.AVAILABLE
        and not server.maintenance.enabled
        and server.reachable
        and server.health.overall in tiers
        # Ungated, unlike min_nic_macs: every caller of this endpoint is
        # creating a BareMetalHost, and none of them can bond a NIC nothing
        # read. `?health=` widens the tier, never this.
        and server.health.network == REQUIRED_NETWORK_HEALTH
    )


class AvailableServersService:
    """Resolves `GET /servers/available` for both its lookup modes."""

    def __init__(
        self,
        *,
        repo: ServerRepository,
        ingest: IngestService,
        provider_factory: Callable[[ManagerType], ServerInventoryProvider],
        capacity_aliases: CapacityAliasCatalog,
    ) -> None:
        """
        Build the service for one request.

        Args:
            repo (ServerRepository): The servers collection.
            ingest (IngestService): Runs a live recheck's fetched record
                through the same pipeline a collection run uses.
            provider_factory (Callable[[ManagerType], ServerInventoryProvider]):
                Builds a provider for one manager type; raises
                `ManagerNotConfiguredError` when this process has no
                credentials for it.
            capacity_aliases (CapacityAliasCatalog): The configured
                `5tb`/`10tb`-style token expansions.
        """
        self._repo = repo
        self._ingest = ingest
        self._provider_factory = provider_factory
        self._capacity_aliases = capacity_aliases
        self._provider_cache: dict[ManagerType, ServerInventoryProvider | None] = {}

    async def lookup_by_name(
        self,
        name: str,
        *,
        extra_filters: dict[str, object],
        tiers: tuple[HealthSeverity, ...] = SELECTABLE_TIERS,
        min_nic_macs: int = 0,
    ) -> AvailableServerResult:
        """
        Resolve `?name=` mode: one exact, live-verified match.

        Args:
            name (str): The exact server name, matched case-insensitively.
            extra_filters (dict[str, object]): Already Mongo-keyed
                `vendor`/`source_provider` filters, if given.
            tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
            min_nic_macs (int): How many NIC MACs must have been read.

        Returns:
            AvailableServerResult: The one matching, still-qualifying server.

        Raises:
            AvailableServersNotFoundError: No server has that name (under
                the given filters), its manager no longer reports it, or
                it no longer qualifies after the live recheck.
            AmbiguousServerNameError: Several documents carry that name.
        """
        normalized = normalize_text(name)
        name_filters = {**extra_filters, "name_normalized": normalized}
        # Counted before fetching: `find_one_by_name` is an unsorted `find_one`,
        # so with several matches it judges an arbitrary one — ADR-0032's
        # 2026-09-25 update.
        match_count = await self._repo.count(name_filters)
        if match_count == 0:
            raise AvailableServersNotFoundError(
                f"no server named {name!r} matches the given filters"
            )
        if match_count > 1:
            raise AmbiguousServerNameError(name, await self._repo.find_ids(name_filters))

        server = await self._repo.find_one_by_name(normalized, filters=extra_filters)
        if server is None:
            raise AvailableServersNotFoundError(
                f"no server named {name!r} matches the given filters"
            )

        rechecked, live_recheck_performed = await self._recheck(server)
        if rechecked is None:
            raise AvailableServersNotFoundError(
                f"{name!r} was found in inventory, but its manager can no longer find it"
            )
        if not server_still_qualifies(rechecked, tiers=tiers, min_nic_macs=min_nic_macs):
            raise AvailableServersNotFoundError(
                f"{name!r} is no longer available: health={rechecked.health.overall.value}, "
                f"openshift={rechecked.openshift.lifecycle_state.value}, "
                f"maintenance={rechecked.maintenance.enabled}, "
                f"reachable={rechecked.reachable}, "
                f"nic_macs={len(rechecked.identity.nic_macs)} "
                f"(unread: {'identity.nic_macs' in rechecked.unread_fields})"
            )
        return AvailableServerResult(
            server=rechecked, live_recheck_performed=live_recheck_performed
        )

    async def lookup_by_pattern(
        self,
        pattern: str,
        *,
        count: int,
        extra_filters: dict[str, object],
        tiers: tuple[HealthSeverity, ...] = SELECTABLE_TIERS,
        min_nic_macs: int = 0,
    ) -> AvailableServersOutcome:
        """
        Resolve `?pattern=` mode: rank, randomly draw, and live-verify up to `count` servers.

        Args:
            pattern (str): A real MongoDB regex against `Server.name`.
            count (int): How many qualifying servers to return.
            extra_filters (dict[str, object]): Already Mongo-keyed
                `vendor`/`source_provider` filters, if given.
            tiers (tuple[HealthSeverity, ...]): Health tiers to draw from, best
                first. Narrowed by `?health=`.
            min_nic_macs (int): How many NIC MACs a candidate must have read.

        Returns:
            AvailableServersOutcome: Up to `count` qualifying servers —
                fewer is honest partial fulfillment, not an error.

        Raises:
            AvailableServersNotFoundError: No name matches `pattern` at
                all, no server survives the vendor/provider filters, or every
                match was unassignable before any live recheck ran.
        """
        name_filter = self._name_filter(pattern)

        # Counted apart from `extra_filters` so the 404 names the clause that
        # emptied the set — ADR-0032's 2026-09-25 update.
        if await self._repo.count(name_filter) == 0:
            raise AvailableServersNotFoundError(f"no server name matches {pattern!r}")

        base_filters = {**name_filter, **extra_filters}
        if extra_filters and await self._repo.count(base_filters) == 0:
            raise AvailableServersNotFoundError(
                f"servers match {pattern!r}, but none of them match the requested "
                f"{', '.join(sorted(extra_filters))} filter"
            )

        assignable_filters = {
            **base_filters,
            "openshift.lifecycle_state": OpenShiftState.AVAILABLE.value,
            "maintenance.enabled": False,
            "reachable": True,
            "health.network": REQUIRED_NETWORK_HEALTH.value,
            **nic_mac_filters(min_nic_macs),
        }
        pre_recheck_count = await self._repo.count(
            {
                **assignable_filters,
                "health.overall": {"$in": [tier.value for tier in tiers]},
            }
        )
        if pre_recheck_count == 0:
            raise AvailableServersNotFoundError(
                f"no server matching {pattern!r} is assignable at health "
                f"{'/'.join(tier.value for tier in tiers)} with at least "
                f"{min_nic_macs} readable NIC MAC(s): every match is unreachable, "
                "in maintenance, already claimed by a cluster, at a worse health "
                "tier, had its NIC MACs unread this collection run, or is not "
                "HEALTHY in the network category (two link-up NICs, actually read)"
            )

        selected = await self._fill_from_tiers(
            assignable_filters, count=count, tiers=tiers, min_nic_macs=min_nic_macs
        )
        return AvailableServersOutcome(items=selected, requested=count)

    def _name_filter(self, pattern: str) -> dict[str, object]:
        """
        Build the Mongo name clause, expanding a capacity-token alias when it applies exactly.

        Args:
            pattern (str): The caller's `?pattern=` value.

        Returns:
            dict[str, object]: `{"name": {"$regex": pattern}}`, or an
                `$or` with the configured alias regex added — never both
                for a pattern that merely contains a token as a substring
                (ADR-0032).
        """
        alias = self._capacity_aliases.expansion_for(pattern)
        if alias is None:
            return {"name": {"$regex": pattern}}
        return {"$or": [{"name": {"$regex": pattern}}, {"name": {"$regex": alias}}]}

    async def _fill_from_tiers(
        self,
        assignable_filters: dict[str, object],
        *,
        count: int,
        tiers: tuple[HealthSeverity, ...] = SELECTABLE_TIERS,
        min_nic_macs: int = 0,
    ) -> list[AvailableServerResult]:
        """
        Draw and live-verify candidates tier by tier until `count` qualify or every tier is spent.

        Args:
            assignable_filters (dict[str, object]): Name/vendor/
                source_provider plus the AVAILABLE/not-maintenance/
                reachable clauses, without a `health.overall` clause.
            count (int): How many qualifying servers to return.
            tiers (tuple[HealthSeverity, ...]): Tiers to draw from, best first.
            min_nic_macs (int): How many NIC MACs a candidate must have read.

        Returns:
            list[AvailableServerResult]: Up to `count` results, best
                tiers filled first.
        """
        selected: list[AvailableServerResult] = []
        tried_ids: set[str] = set()

        for tier in tiers:
            remaining = count - len(selected)
            if remaining <= 0:
                break
            for _round in range(_MAX_REPLACEMENT_ROUNDS_PER_TIER):
                remaining = count - len(selected)
                if remaining <= 0:
                    break
                candidates = await self._repo.sample_available_tier(
                    filters=assignable_filters,
                    severity=tier.value,
                    size=remaining,
                    exclude_ids=tuple(tried_ids),
                )
                if not candidates:
                    break
                for candidate in candidates:
                    tried_ids.add(candidate.id)
                    rechecked, live_recheck_performed = await self._recheck(candidate)
                    if rechecked is not None and server_still_qualifies(
                        rechecked, tiers=tiers, min_nic_macs=min_nic_macs
                    ):
                        selected.append(
                            AvailableServerResult(
                                server=rechecked, live_recheck_performed=live_recheck_performed
                            )
                        )
                        if len(selected) >= count:
                            break
        return selected

    async def _recheck(self, server: Server) -> tuple[Server | None, bool]:
        """
        Live-verify one server, persisting the fresh state through `IngestService`.

        Degrades to trusting `server` unchanged per ADR-0032, Decision 5.

        Args:
            server (Server): The stored candidate.

        Returns:
            tuple[Server | None, bool]: The current server (fresh if
                rechecked, `server` itself if the recheck was skipped, or
                `None` if the manager can no longer find it at all), and
                whether a live recheck actually ran.
        """
        if server.source_provider is None:
            return server, False

        manager_type = ManagerType(server.source_provider)
        provider = self._provider_for(manager_type)
        if provider is None:
            return server, False

        identity = ServerIdentity(
            serial=server.identity.serial,
            external_id=server.identity.external_ids.get(server.manager_id or ""),
            host=server.network.bmc.host,
            name=server.name,
        )
        fresh = await provider.get_one(identity)
        if fresh is None:
            return None, True
        updated = await self._ingest.ingest_one(fresh, provider_type=manager_type.value)
        return updated, True

    def _provider_for(self, manager_type: ManagerType) -> ServerInventoryProvider | None:
        """
        This request's provider for `manager_type`, built and cached once.

        Args:
            manager_type (ManagerType): The collector to build.

        Returns:
            ServerInventoryProvider | None: The provider, or `None` when
                this process has no credentials configured for it.
        """
        if manager_type not in self._provider_cache:
            try:
                self._provider_cache[manager_type] = self._provider_factory(manager_type)
            except ManagerNotConfiguredError:
                self._provider_cache[manager_type] = None
        return self._provider_cache[manager_type]
