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

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import structlog
from pydantic import TypeAdapter

from app.application.services.ingest import IngestService
from app.domain.enums import HealthSeverity, ManagerType, OpenShiftState
from app.domain.models.server import Server
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.ports.provider import ServerIdentity, ServerInventoryProvider
from app.domain.ports.repository import ServerRepository
from app.domain.services.normalize import normalize_text
from app.domain.value_objects.capacity_aliases import CapacityAliasCatalog
from app.observability.metrics import dependency_call_duration_seconds
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

# Best to worst; never CRITICAL, and UNKNOWN (never evaluated) is excluded too (ADR-0027).
SELECTABLE_TIERS: tuple[HealthSeverity, ...] = (
    HealthSeverity.HEALTHY,
    HealthSeverity.WARNING,
    HealthSeverity.MAJOR,
)

_SELECTABLE_VALUES = [tier.value for tier in SELECTABLE_TIERS]

# `health.overall` alone cannot carry this: OneView never reads links and a server with no
# interface listed still roll up HEALTHY — docs/adr/0032 (2026-09-26, 2026-10-21).
REQUIRED_NETWORK_HEALTH = HealthSeverity.HEALTHY

# Reasons only this platform's own database decides: a vendor recheck cannot change them.
_STORED_ONLY_CODES = frozenset({"INSTALLED", "CLAIMED", "MAINTENANCE", "RESERVED"})

# Bounds worst-case cost when candidates keep failing their live recheck —
# ADR-0032, "candidate ranking".
_MAX_REPLACEMENT_ROUNDS_PER_TIER = 3


class AvailableServersNotFoundError(Exception):
    """No server could be returned at all; `reason` is the human-readable detail."""

    def __init__(self, reason: str, codes: tuple[str, ...] = ()) -> None:
        """
        Build the error.

        Args:
            reason (str): What made the result empty, for the route's
                RFC 9457 `detail`.
            codes (tuple[str, ...]): Machine-readable reasons (`INSTALLED`,
                `MAINTENANCE`, ...) for the response's `details.reasons`.
        """
        super().__init__(reason)
        self.reason = reason
        self.codes = codes


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


class VendorRecheckError(Exception):
    """A vendor manager failed (timeout, login, network) during a live recheck.

    ADR-0032, 2026-10-11 update: raised for the `get_one()` call only, so the route
    can answer 503 naming the vendor instead of an unexplained 500.
    """

    def __init__(self, manager_type: ManagerType, server_name: str, reason: str) -> None:
        """
        Build the error.

        Args:
            manager_type (ManagerType): The collector whose manager failed.
            server_name (str): The server being rechecked.
            reason (str): The underlying failure, class name included.
        """
        super().__init__(f"{manager_type.value} recheck of {server_name!r} failed: {reason}")
        self.manager_type = manager_type
        self.server_name = server_name
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


def unreserved_filters(now: datetime) -> dict[str, object]:
    """
    The Mongo clause excluding servers currently reserved for an install.

    `$nor` not `$or`, and the cutoff as an ISO STRING — ADR-0035, decision 7.

    Args:
        now (datetime): The instant expiry is judged against.

    Returns:
        dict[str, object]: A clause to merge into an assignability filter. Owns
            the `$nor` key, which nothing else in this module uses.
    """
    # Documents store datetimes as ISO STRINGS (`model_dump(mode="json")`), and
    # a `datetime` compared against one matches NOTHING — different BSON types.
    # `fleet_snapshot` converts its cutoff the same way.
    cutoff = TypeAdapter(datetime).dump_python(now, mode="json")
    return {
        "$nor": [
            {"reservation.holder": {"$ne": None}, "reservation.expires_at": None},
            {
                "reservation.holder": {"$ne": None},
                "reservation.expires_at": {"$gt": cutoff},
            },
        ]
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
        # Agrees with unreserved_filters, as ADR-0032 requires of every clause
        # here: a machine another run is installing is not assignable, however
        # healthy it looks.
        and not server.reservation.is_live()
    )


def unassignable_reasons(
    server: Server,
    *,
    tiers: tuple[HealthSeverity, ...] = SELECTABLE_TIERS,
    min_nic_macs: int = 0,
) -> list[tuple[str, str]]:
    """
    Why a server is not assignable, one `(code, sentence)` per failed condition.

    The explanatory twin of `server_still_qualifies`: empty exactly when that is `True`.

    Args:
        server (Server): The server, as freshly rechecked (or last stored).
        tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
        min_nic_macs (int): How many NIC MACs must have been read.

    Returns:
        list[tuple[str, str]]: Machine code and plain-English reason, most decisive first.
    """
    reasons: list[tuple[str, str]] = []
    state = server.openshift.lifecycle_state
    if state == OpenShiftState.INSTALLED:
        cluster = server.openshift.cluster_name
        reasons.append(
            (
                "INSTALLED",
                f"it is already installed in cluster {cluster!r}"
                if cluster
                else "it is already installed in a cluster",
            )
        )
    elif state != OpenShiftState.AVAILABLE:
        mce = server.openshift.mce_name
        reasons.append(
            (
                "CLAIMED",
                f"it is already claimed by MCE {mce!r} ({state.value})"
                if mce
                else f"its OpenShift state is {state.value}, not AVAILABLE",
            )
        )
    if server.maintenance.enabled:
        why = server.maintenance.reason
        reasons.append(
            ("MAINTENANCE", f"it is in maintenance ({why})" if why else "it is in maintenance")
        )
    if not server.reachable:
        cause = server.unreachable_reason
        reasons.append(
            (
                "UNREACHABLE",
                f"its BMC is unreachable ({cause.value})" if cause else "its BMC is unreachable",
            )
        )
    if server.reservation.is_live():
        holder = server.reservation.holder or "an install"
        reasons.append(("RESERVED", f"it is reserved by {holder} for an install in progress"))
    if server.health.overall not in tiers:
        reasons.append(
            (
                "HEALTH",
                f"its health is {server.health.overall.value}, outside the requested "
                f"{'/'.join(tier.value for tier in tiers)}",
            )
        )
    if server.health.network != REQUIRED_NETWORK_HEALTH:
        reasons.append(
            (
                "NETWORK",
                f"its network health is {server.health.network.value}, not "
                f"{REQUIRED_NETWORK_HEALTH.value} (two link-up NICs actually read)",
            )
        )
    if min_nic_macs >= 1 and (
        len(server.identity.nic_macs) < min_nic_macs or "identity.nic_macs" in server.unread_fields
    ):
        reasons.append(("NIC_MACS", f"fewer than {min_nic_macs} NIC MAC(s) were read"))
    return reasons


def _reason_message(subject: str, reasons: list[tuple[str, str]]) -> AvailableServersNotFoundError:
    """
    Build the not-found error for one server from its reasons.

    Args:
        subject (str): The quoted server name.
        reasons (list[tuple[str, str]]): Output of `unassignable_reasons`.

    Returns:
        AvailableServersNotFoundError: Detail `'<name>' is not available: <reason>; <reason>`.
    """
    return AvailableServersNotFoundError(
        f"{subject} is not available: " + "; ".join(text for _code, text in reasons),
        tuple(code for code, _text in reasons),
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
            VendorRecheckError: Its vendor manager failed during the live recheck.
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

        # No vendor call can change these three, so answer from the stored document
        # first: an installed server is "installed", even while its manager is down.
        stored_only = [
            reason
            for reason in unassignable_reasons(server, tiers=tiers, min_nic_macs=0)
            if reason[0] in _STORED_ONLY_CODES
        ]
        if stored_only:
            raise _reason_message(repr(name), stored_only)

        rechecked, live_recheck_performed = await self._recheck(server)
        if rechecked is None:
            raise AvailableServersNotFoundError(
                f"{name!r} was found in inventory, but its manager can no longer find it",
                ("GONE_FROM_MANAGER",),
            )
        if not server_still_qualifies(rechecked, tiers=tiers, min_nic_macs=min_nic_macs):
            raise _reason_message(
                repr(name),
                unassignable_reasons(rechecked, tiers=tiers, min_nic_macs=min_nic_macs),
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
            VendorRecheckError: Every drawn candidate's vendor manager failed, so
                nothing could be verified (a partial fill is returned instead).
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
            **unreserved_filters(utcnow()),
        }
        pre_recheck_count = await self._repo.count(
            {
                **assignable_filters,
                "health.overall": {"$in": [tier.value for tier in tiers]},
            }
        )
        if pre_recheck_count == 0:
            raise await self._explain_none_assignable(
                pattern, base_filters, tiers=tiers, min_nic_macs=min_nic_macs
            )

        selected = await self._fill_from_tiers(
            assignable_filters, count=count, tiers=tiers, min_nic_macs=min_nic_macs
        )
        return AvailableServersOutcome(items=selected, requested=count)

    async def _explain_none_assignable(
        self,
        pattern: str,
        base_filters: dict[str, object],
        *,
        tiers: tuple[HealthSeverity, ...],
        min_nic_macs: int,
    ) -> AvailableServersNotFoundError:
        """
        Say why every server matching `pattern` is excluded, with a count per reason.

        Each match is counted once, under the first reason that applies, so the
        counts add up to the number of matches.

        Args:
            pattern (str): The caller's pattern, quoted in the message.
            base_filters (dict[str, object]): The name/vendor/source_provider filters.
            tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
            min_nic_macs (int): How many NIC MACs a candidate must have read.

        Returns:
            AvailableServersNotFoundError: Detail like `3 servers match 'x', but none
                is available: 2 already installed, 1 in maintenance`.
        """
        reserved = {"$or": unreserved_filters(utcnow())["$nor"]}
        steps: list[tuple[str, str, dict[str, object]]] = [
            (
                "INSTALLED",
                "installed in a cluster",
                {"openshift.lifecycle_state": OpenShiftState.INSTALLED.value},
            ),
            (
                "CLAIMED",
                "claimed by an MCE",
                {"openshift.lifecycle_state": OpenShiftState.INSTALLED_TO_INVENTORY.value},
            ),
            ("MAINTENANCE", "in maintenance", {"maintenance.enabled": True}),
            ("UNREACHABLE", "unreachable", {"reachable": False}),
            ("RESERVED", "reserved by an install in progress", reserved),
            (
                "HEALTH",
                f"outside the requested health ({'/'.join(tier.value for tier in tiers)})",
                {"health.overall": {"$nin": [tier.value for tier in tiers]}},
            ),
            (
                "NETWORK",
                "without a healthy, actually-read network",
                {"health.network": {"$ne": REQUIRED_NETWORK_HEALTH.value}},
            ),
        ]
        if min_nic_macs >= 1:
            steps.append(
                (
                    "NIC_MACS",
                    f"with fewer than {min_nic_macs} NIC MAC(s) read",
                    {"$nor": [nic_mac_filters(min_nic_macs)]},
                )
            )

        total = await self._repo.count(base_filters)
        counted: list[tuple[str, str, int]] = []
        already: list[dict[str, object]] = []
        for code, text, condition in steps:
            clauses: list[dict[str, object]] = [base_filters, condition]
            if already:
                clauses.append({"$nor": list(already)})
            hits = await self._repo.count({"$and": clauses})
            already.append(condition)
            if hits:
                counted.append((code, text, hits))
        plural = "server matches" if total == 1 else "servers match"
        breakdown = ", ".join(f"{hits} {text}" for _code, text, hits in counted) or "none qualify"
        return AvailableServersNotFoundError(
            f"{total} {plural} {pattern!r}, but none is available: {breakdown}",
            tuple(code for code, _text, _hits in counted),
        )

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
        disqualified = 0
        recheck_failures: list[VendorRecheckError] = []

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
                    try:
                        rechecked, live_recheck_performed = await self._recheck(candidate)
                    except VendorRecheckError as exc:
                        recheck_failures.append(exc)
                        continue
                    qualifies = rechecked is not None and server_still_qualifies(
                        rechecked, tiers=tiers, min_nic_macs=min_nic_macs
                    )
                    if not qualifies:
                        disqualified += 1
                        logger.info(
                            "available.candidate_rejected",
                            server=candidate.name,
                            tier=tier.value,
                            gone_from_manager=rechecked is None,
                        )
                    if rechecked is not None and qualifies:
                        selected.append(
                            AvailableServerResult(
                                server=rechecked, live_recheck_performed=live_recheck_performed
                            )
                        )
                        if len(selected) >= count:
                            break
        logger.info(
            "available.filled",
            requested=count,
            returned=len(selected),
            candidates_tried=len(tried_ids),
            rejected_after_recheck=disqualified,
            vendor_failures=len(recheck_failures),
        )
        if not selected and recheck_failures:
            # Nothing could be verified because the vendor failed, which is not the
            # same answer as "nothing is available": surface it (503), not a 404.
            raise recheck_failures[0]
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
            logger.info("available.recheck", server=server.name, outcome="skipped_no_source")
            return server, False

        manager_type = ManagerType(server.source_provider)
        provider = self._provider_for(manager_type)
        if provider is None:
            logger.info(
                "available.recheck",
                server=server.name,
                manager_type=manager_type.value,
                outcome="skipped_not_configured",
            )
            return server, False

        identity = ServerIdentity(
            serial=server.identity.serial,
            external_id=server.identity.external_ids.get(server.manager_id or ""),
            host=server.network.bmc.host,
            name=server.name,
        )
        started = time.monotonic()
        outcome = "failed"
        try:
            try:
                fresh = await provider.get_one(identity)
            except Exception as exc:
                logger.exception(
                    "available.recheck_failed", server=server.name, manager_type=manager_type.value
                )
                raise VendorRecheckError(
                    manager_type, server.name, f"{type(exc).__name__}: {exc}"
                ) from exc
            if fresh is None:
                outcome = "not_found"
                return None, True
            updated = await self._ingest.ingest_one(fresh, provider_type=manager_type.value)
            outcome = "verified"
            return updated, True
        finally:
            elapsed = time.monotonic() - started
            dependency_call_duration_seconds.labels(
                dependency=f"vendor_{manager_type.value.lower()}", outcome=outcome
            ).observe(elapsed)
            logger.info(
                "available.recheck",
                server=server.name,
                manager_type=manager_type.value,
                outcome=outcome,
                duration_ms=round(elapsed * 1000, 1),
            )

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
