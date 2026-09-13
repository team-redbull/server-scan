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


def server_still_qualifies(server: Server) -> bool:
    """
    Whether a server's current state still makes it assignable.

    The same predicate both modes apply after a live recheck (ADR-0032).

    Args:
        server (Server): The server, as freshly rechecked (or, on a
            degraded recheck, as last stored).

    Returns:
        bool: `True` when it is `AVAILABLE`, not in maintenance,
            reachable, and its overall health is in `SELECTABLE_TIERS`.
    """
    return (
        server.openshift.lifecycle_state == OpenShiftState.AVAILABLE
        and not server.maintenance.enabled
        and server.reachable
        and server.health.overall in SELECTABLE_TIERS
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
        self, name: str, *, extra_filters: dict[str, object]
    ) -> AvailableServerResult:
        """
        Resolve `?name=` mode: one exact, live-verified match.

        Args:
            name (str): The exact server name, matched case-insensitively.
            extra_filters (dict[str, object]): Already Mongo-keyed
                `vendor`/`source_provider` filters, if given.

        Returns:
            AvailableServerResult: The one matching, still-qualifying server.

        Raises:
            AvailableServersNotFoundError: No server has that name (under
                the given filters), its manager no longer reports it, or
                it no longer qualifies after the live recheck.
        """
        server = await self._repo.find_one_by_name(normalize_text(name), filters=extra_filters)
        if server is None:
            raise AvailableServersNotFoundError(
                f"no server named {name!r} matches the given filters"
            )

        rechecked, live_recheck_performed = await self._recheck(server)
        if rechecked is None:
            raise AvailableServersNotFoundError(
                f"{name!r} was found in inventory, but its manager can no longer find it"
            )
        if not server_still_qualifies(rechecked):
            raise AvailableServersNotFoundError(
                f"{name!r} is no longer available: health={rechecked.health.overall.value}, "
                f"openshift={rechecked.openshift.lifecycle_state.value}, "
                f"maintenance={rechecked.maintenance.enabled}, reachable={rechecked.reachable}"
            )
        return AvailableServerResult(
            server=rechecked, live_recheck_performed=live_recheck_performed
        )

    async def lookup_by_pattern(
        self, pattern: str, *, count: int, extra_filters: dict[str, object]
    ) -> AvailableServersOutcome:
        """
        Resolve `?pattern=` mode: rank, randomly draw, and live-verify up to `count` servers.

        Args:
            pattern (str): A real MongoDB regex against `Server.name`.
            count (int): How many qualifying servers to return.
            extra_filters (dict[str, object]): Already Mongo-keyed
                `vendor`/`source_provider` filters, if given.

        Returns:
            AvailableServersOutcome: Up to `count` qualifying servers —
                fewer is honest partial fulfillment, not an error.

        Raises:
            AvailableServersNotFoundError: No name matches `pattern` at
                all, or every match was unassignable/CRITICAL/UNKNOWN
                even before any live recheck ran.
        """
        name_filter = self._name_filter(pattern)
        base_filters = {**name_filter, **extra_filters}

        if await self._repo.count(base_filters) == 0:
            raise AvailableServersNotFoundError(f"no server name matches {pattern!r}")

        assignable_filters = {
            **base_filters,
            "openshift.lifecycle_state": OpenShiftState.AVAILABLE.value,
            "maintenance.enabled": False,
            "reachable": True,
        }
        pre_recheck_count = await self._repo.count(
            {**assignable_filters, "health.overall": {"$in": _SELECTABLE_VALUES}}
        )
        if pre_recheck_count == 0:
            raise AvailableServersNotFoundError(
                f"every server matching {pattern!r} is CRITICAL, UNKNOWN, unreachable, "
                "in maintenance, or already claimed by a cluster"
            )

        selected = await self._fill_from_tiers(assignable_filters, count=count)
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
        self, assignable_filters: dict[str, object], *, count: int
    ) -> list[AvailableServerResult]:
        """
        Draw and live-verify candidates tier by tier until `count` qualify or every tier is spent.

        Args:
            assignable_filters (dict[str, object]): Name/vendor/
                source_provider plus the AVAILABLE/not-maintenance/
                reachable clauses, without a `health.overall` clause.
            count (int): How many qualifying servers to return.

        Returns:
            list[AvailableServerResult]: Up to `count` results, best
                tiers filled first.
        """
        selected: list[AvailableServerResult] = []
        tried_ids: set[str] = set()

        for tier in SELECTABLE_TIERS:
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
                    if rechecked is not None and server_still_qualifies(rechecked):
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
