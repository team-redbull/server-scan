"""`/servers/available`: Mongo-side candidate resolution, then (POST only) lock and live check.

`GET` answers from MongoDB alone: it ranks the candidates and returns them without a live
recheck or a lock. `POST` draws one candidate at a time, locks it, live-checks it while it holds
the lock, and either returns it or releases it and tries the next, up to a bounded number of
attempts. See docs/adr/0063-available-reserves-and-verifies.md; the draw, the health-tier order
and the degrade-to-stored-document rule for an unconfigured manager are
docs/adr/0032-available-server-lookup-api.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from pydantic import TypeAdapter

from app.application.services.ingest import IngestService
from app.application.services.reservation_service import ReservationClaim, ReservationService
from app.domain.enums import HealthSeverity, ManagerType, OpenShiftState
from app.domain.models.audit_event import Actor
from app.domain.models.server import Server
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.ports.provider import (
    NotThisManagersServerError,
    ServerIdentity,
    ServerInventoryProvider,
)
from app.domain.ports.repository import ServerRepository
from app.domain.services.normalize import normalize_text
from app.domain.value_objects.capacity_aliases import CapacityAliasCatalog
from app.errors import ConflictError
from app.observability.metrics import dependency_call_duration_seconds
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

# Best to worst; never CRITICAL, and UNKNOWN (never evaluated) is excluded too (ADR-0027).
SELECTABLE_TIERS: tuple[HealthSeverity, ...] = (
    HealthSeverity.HEALTHY,
    HealthSeverity.WARNING,
    HealthSeverity.MAJOR,
)

# `max_health` picks a prefix of this, best first; UNKNOWN is in none of them (ADR-0063).
HEALTH_ORDER: tuple[HealthSeverity, ...] = (
    HealthSeverity.HEALTHY,
    HealthSeverity.WARNING,
    HealthSeverity.MAJOR,
    HealthSeverity.CRITICAL,
)


def tiers_up_to(max_health: HealthSeverity) -> tuple[HealthSeverity, ...]:
    """
    The health tiers a `max_health` allows, best first.

    Args:
        max_health (HealthSeverity): The worst acceptable tier.

    Returns:
        tuple[HealthSeverity, ...]: That tier and every better one; `()` for UNKNOWN or INFO.
    """
    if max_health not in HEALTH_ORDER:
        return ()
    return HEALTH_ORDER[: HEALTH_ORDER.index(max_health) + 1]


# `health.overall` alone cannot carry this: OneView never reads links and a server with no
# interface listed still roll up HEALTHY — docs/adr/0032 (2026-09-26, 2026-10-21).
REQUIRED_NETWORK_HEALTH = HealthSeverity.HEALTHY

# Reasons only this platform's own database decides: a vendor recheck cannot change them.
_STORED_ONLY_CODES = frozenset({"INSTALLED", "CLAIMED", "MAINTENANCE", "RESERVED"})

# Candidates another request locked first, which cost no vendor call; bounded so a hot pool
# cannot loop forever (ADR-0063).
_MAX_CLAIM_RACES = 20

# The probe lease must outlive the vendor call it guards: `recheck timeout + slack`, at least this.
_MIN_PROBE_LEASE_SECONDS = 120.0
_PROBE_LEASE_SLACK_SECONDS = 60.0
_DEFAULT_RECHECK_TIMEOUT_SECONDS = 15.0

# A vendor's exception text reaches the 503 in `detail` and `details.reason` (ADR-0002, 2026-11-09).
_MAX_RECHECK_REASON_CHARS = 500


def recheck_failure_reason(exc: Exception) -> str:
    """
    Name a vendor failure for the 503, in at most `_MAX_RECHECK_REASON_CHARS` characters.

    Args:
        exc (Exception): What the provider's `get_one()` raised; its full text is logged.

    Returns:
        str: `<ExceptionType>: <message>`, cut with a marker when too long.
    """
    reason = f"{type(exc).__name__}: {exc}"
    if len(reason) <= _MAX_RECHECK_REASON_CHARS:
        return reason
    cut = len(reason) - _MAX_RECHECK_REASON_CHARS
    return (
        f"{reason[:_MAX_RECHECK_REASON_CHARS]}… [{cut:,} more characters in the "
        "available.recheck_failed log line]"
    )


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
    own_claim: ReservationClaim | None = None,
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
        own_claim (ReservationClaim | None): A lock held by this claim does not count against
            the server (a POST judges the server it has just locked).

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
        and not _reserved_by_another(server, own_claim)
    )


def _reserved_by_another(server: Server, own_claim: ReservationClaim | None) -> bool:
    """
    Whether a live reservation on `server` is somebody else's.

    Args:
        server (Server): The server.
        own_claim (ReservationClaim | None): The claim whose lock does not count.

    Returns:
        bool: True when it is reserved and the lock is not `own_claim`'s.
    """
    if not server.reservation.is_live():
        return False
    return own_claim is None or not own_claim.owns(server.reservation)


def unassignable_reasons(
    server: Server,
    *,
    tiers: tuple[HealthSeverity, ...] = SELECTABLE_TIERS,
    min_nic_macs: int = 0,
    own_claim: ReservationClaim | None = None,
) -> list[tuple[str, str]]:
    """
    Why a server is not assignable, one `(code, sentence)` per failed condition.

    The explanatory twin of `server_still_qualifies`: empty exactly when that is `True`.

    Args:
        server (Server): The server, as freshly rechecked (or last stored).
        tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
        min_nic_macs (int): How many NIC MACs must have been read.
        own_claim (ReservationClaim | None): A lock held by this claim is not a reason.

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
    if _reserved_by_another(server, own_claim):
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


@dataclass(slots=True)
class _Tried:
    """What one `POST` has done so far: the ids it drew and why each failed."""

    ids: set[str] = field(default_factory=set)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    attempts: int = 0
    races: int = 0
    stuck: bool = False


class AvailableServersService:
    """Resolves `/servers/available`: `GET` reads Mongo, `POST` locks, checks and returns."""

    def __init__(
        self,
        *,
        repo: ServerRepository,
        ingest: IngestService,
        provider_factory: Callable[[ManagerType], ServerInventoryProvider],
        capacity_aliases: CapacityAliasCatalog,
        reservations: ReservationService,
        max_attempts: int = 5,
        recheck_timeout: Callable[[ManagerType], float] = lambda _mt: (
            _DEFAULT_RECHECK_TIMEOUT_SECONDS
        ),
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
            reservations (ReservationService): Locks and releases a candidate (`POST`).
            max_attempts (int): Candidates one `POST` locks and live-checks before giving up.
            recheck_timeout (Callable[[ManagerType], float]): The longest a manager type's live
                recheck may run, in seconds, which sizes the probe lease.
        """
        self._repo = repo
        self._ingest = ingest
        self._provider_factory = provider_factory
        self._capacity_aliases = capacity_aliases
        self._reservations = reservations
        self._max_attempts = max_attempts
        self._recheck_timeout = recheck_timeout
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
        Resolve `GET ?name=`: one exact match, judged on its stored document.

        Args:
            name (str): The exact server name, matched case-insensitively.
            extra_filters (dict[str, object]): Already Mongo-keyed
                `vendor`/`source_provider` filters, if given.
            tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
            min_nic_macs (int): How many NIC MACs must have been read.

        Returns:
            AvailableServerResult: The one matching server; no live recheck ran.

        Raises:
            AvailableServersNotFoundError: No server has that name (under the given filters)
                or it is not assignable as stored.
            AmbiguousServerNameError: Several documents carry that name.
        """
        server = await self._find_named(name, extra_filters)
        if not server_still_qualifies(server, tiers=tiers, min_nic_macs=min_nic_macs):
            raise _reason_message(
                repr(name),
                unassignable_reasons(server, tiers=tiers, min_nic_macs=min_nic_macs),
            )
        return AvailableServerResult(server=server, live_recheck_performed=False)

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
        Resolve `GET ?pattern=`: rank, randomly draw up to `count` servers, no recheck, no lock.

        Args:
            pattern (str): A real MongoDB regex against `Server.name`.
            count (int): How many qualifying servers to return.
            extra_filters (dict[str, object]): Already Mongo-keyed
                `vendor`/`source_provider` filters, if given.
            tiers (tuple[HealthSeverity, ...]): Health tiers to draw from, best first.
            min_nic_macs (int): How many NIC MACs a candidate must have read.

        Returns:
            AvailableServersOutcome: Up to `count` servers; fewer is honest partial fulfillment.

        Raises:
            AvailableServersNotFoundError: No name matches `pattern` at all, no server survives
                the vendor/provider filters, or every match was unassignable.
        """
        assignable_filters = await self._pattern_filters(
            pattern, extra_filters, tiers=tiers, min_nic_macs=min_nic_macs
        )
        selected: list[AvailableServerResult] = []
        for tier in tiers:
            remaining = count - len(selected)
            if remaining <= 0:
                break
            drawn = await self._repo.sample_available_tier(
                filters=assignable_filters, severity=tier.value, size=remaining
            )
            selected.extend(
                AvailableServerResult(server=server, live_recheck_performed=False)
                for server in drawn
            )
        logger.info("available.filled", requested=count, returned=len(selected), locked=False)
        return AvailableServersOutcome(items=selected, requested=count)

    async def reserve_by_name(
        self,
        name: str,
        *,
        claim: ReservationClaim,
        actor: Actor,
        request_id: str | None,
        extra_filters: dict[str, object],
        tiers: tuple[HealthSeverity, ...],
        min_nic_macs: int = 0,
    ) -> AvailableServerResult:
        """
        Resolve `POST name=`: lock the one exact match, live-check it, return it or fail (ADR-0063).

        Args:
            name (str): The exact server name, matched case-insensitively.
            claim (ReservationClaim): Who is taking the server and for how long.
            actor (Actor): Who is reserving, for the audit event.
            request_id (str | None): The originating API request id, if any.
            extra_filters (dict[str, object]): Already Mongo-keyed `vendor`/`source_provider`.
            tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
            min_nic_macs (int): How many NIC MACs must have been read.

        Returns:
            AvailableServerResult: The locked, live-checked server.

        Raises:
            AvailableServersNotFoundError: No such server, it is not assignable as stored, or it
                failed its live check or was locked by another request.
            AmbiguousServerNameError: Several documents carry that name.
        """
        server = await self._find_named(name, extra_filters)
        # No vendor call can change these, so answer from the stored document first: an
        # installed server is "installed", even while its manager is down.
        stored_only = [
            reason
            for reason in unassignable_reasons(server, tiers=tiers, min_nic_macs=0)
            if reason[0] in _STORED_ONLY_CODES
        ]
        if stored_only:
            raise _reason_message(repr(name), stored_only)
        tried = _Tried()
        began = time.monotonic()
        result = await self._lock_and_verify(
            server, claim, tried, actor, request_id, tiers=tiers, min_nic_macs=min_nic_macs
        )
        logger.info(
            "available.request_timing",
            mode="name",
            reserved=result is not None,
            attempts=tried.attempts,
            total_ms=round((time.monotonic() - began) * 1000),
        )
        if result is None:
            raise self._exhausted(repr(name), tried)
        return result

    async def reserve_by_pattern(
        self,
        pattern: str,
        *,
        claim: ReservationClaim,
        actor: Actor,
        request_id: str | None,
        extra_filters: dict[str, object],
        tiers: tuple[HealthSeverity, ...],
        min_nic_macs: int = 0,
    ) -> AvailableServerResult:
        """
        Resolve `POST pattern=`: lock, live-check and return one server, trying up to the cap.

        One candidate at a time, best health first: it is locked, then checked while locked, and
        either returned or released before the next is drawn (ADR-0063).

        Args:
            pattern (str): A real MongoDB regex against `Server.name`.
            claim (ReservationClaim): Who is taking the server and for how long.
            actor (Actor): Who is reserving, for the audit event.
            request_id (str | None): The originating API request id, if any.
            extra_filters (dict[str, object]): Already Mongo-keyed `vendor`/`source_provider`.
            tiers (tuple[HealthSeverity, ...]): Acceptable health tiers, best first.
            min_nic_macs (int): How many NIC MACs a candidate must have read.

        Returns:
            AvailableServerResult: The locked, live-checked server.

        Raises:
            AvailableServersNotFoundError: Nothing matched or qualified, every candidate failed its
                live check, was taken by another request, or the attempt cap was reached.
        """
        assignable_filters = await self._pattern_filters(
            pattern, extra_filters, tiers=tiers, min_nic_macs=min_nic_macs
        )
        tried = _Tried()
        began = time.monotonic()
        while (
            tried.attempts < self._max_attempts
            and tried.races < _MAX_CLAIM_RACES
            and not tried.stuck
        ):
            candidate = await self._next_candidate(assignable_filters, tiers, tried.ids)
            if candidate is None:
                break
            result = await self._lock_and_verify(
                candidate, claim, tried, actor, request_id, tiers=tiers, min_nic_macs=min_nic_macs
            )
            if result is not None:
                logger.info(
                    "available.reserved",
                    server=result.server.name,
                    attempts=tried.attempts,
                    races=tried.races,
                    total_ms=round((time.monotonic() - began) * 1000),
                )
                return result
        logger.info(
            "available.request_timing",
            mode="pattern",
            reserved=False,
            attempts=tried.attempts,
            races=tried.races,
            total_ms=round((time.monotonic() - began) * 1000),
        )
        raise self._exhausted(repr(pattern), tried)

    async def _find_named(self, name: str, extra_filters: dict[str, object]) -> Server:
        """
        The one server called `name` under the filters.

        Args:
            name (str): The exact name, matched case-insensitively.
            extra_filters (dict[str, object]): Already Mongo-keyed vendor/provider filters.

        Returns:
            Server: The match.

        Raises:
            AvailableServersNotFoundError: No server has that name under the filters.
            AmbiguousServerNameError: Several documents carry that name.
        """
        normalized = normalize_text(name)
        name_filters = {**extra_filters, "name_normalized": normalized}
        # Counted before fetching: `find_one_by_name` is an unsorted `find_one`, so with several
        # matches it judges an arbitrary one (ADR-0032, 2026-09-25 update).
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
        return server

    async def _pattern_filters(
        self,
        pattern: str,
        extra_filters: dict[str, object],
        *,
        tiers: tuple[HealthSeverity, ...],
        min_nic_macs: int,
    ) -> dict[str, object]:
        """
        The Mongo filter for a pattern lookup, after proving at least one server can match it.

        Args:
            pattern (str): The caller's regex.
            extra_filters (dict[str, object]): Already Mongo-keyed vendor/provider filters.
            tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
            min_nic_macs (int): How many NIC MACs a candidate must have read.

        Returns:
            dict[str, object]: Name, vendor, provider and assignability clauses, without health.

        Raises:
            AvailableServersNotFoundError: Nothing matches, or nothing assignable does.
        """
        name_filter = self._name_filter(pattern)

        # Counted apart from `extra_filters` so the message names the clause that emptied the
        # set (ADR-0032, 2026-09-25 update).
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
        drawable = await self._repo.count(
            {**assignable_filters, "health.overall": {"$in": [tier.value for tier in tiers]}}
        )
        if drawable == 0:
            raise await self._explain_none_assignable(
                pattern, base_filters, tiers=tiers, min_nic_macs=min_nic_macs
            )
        return assignable_filters

    async def _next_candidate(
        self,
        assignable_filters: dict[str, object],
        tiers: tuple[HealthSeverity, ...],
        tried_ids: set[str],
    ) -> Server | None:
        """
        Draw one not-yet-tried candidate from the best tier that has one.

        Args:
            assignable_filters (dict[str, object]): The assignability filter, without health.
            tiers (tuple[HealthSeverity, ...]): Acceptable tiers, best first.
            tried_ids (set[str]): Ids already drawn by this request.

        Returns:
            Server | None: A random candidate of the best non-empty tier, or `None`.
        """
        for tier in tiers:
            drawn = await self._repo.sample_available_tier(
                filters=assignable_filters,
                severity=tier.value,
                size=1,
                exclude_ids=tuple(tried_ids),
            )
            if drawn:
                return drawn[0]
        return None

    def _probe_lease(self, server: Server) -> timedelta:
        """
        How long a candidate's provisional lock lasts: longer than the vendor call it guards.

        Args:
            server (Server): The candidate.

        Returns:
            timedelta: `recheck timeout + slack`, at least the minimum lease.
        """
        timeout = _DEFAULT_RECHECK_TIMEOUT_SECONDS
        if server.source_provider is not None:
            with contextlib.suppress(ValueError):
                timeout = self._recheck_timeout(ManagerType(server.source_provider))
        return timedelta(
            seconds=max(_MIN_PROBE_LEASE_SECONDS, timeout + _PROBE_LEASE_SLACK_SECONDS)
        )

    async def _lock_and_verify(
        self,
        candidate: Server,
        claim: ReservationClaim,
        tried: _Tried,
        actor: Actor,
        request_id: str | None,
        *,
        tiers: tuple[HealthSeverity, ...],
        min_nic_macs: int,
    ) -> AvailableServerResult | None:
        """
        Lock one candidate, live-check it while locked, and keep the lock only if it passes.

        A lost race costs no attempt. Any other outcome that is not a pass releases the lock
        before returning, and so does an exception or a cancellation.

        Args:
            candidate (Server): The drawn candidate.
            claim (ReservationClaim): Who is taking it.
            tried (_Tried): This request's running record, updated in place.
            actor (Actor): Who is reserving, for the audit event.
            request_id (str | None): The originating API request id, if any.
            tiers (tuple[HealthSeverity, ...]): Acceptable health tiers.
            min_nic_macs (int): How many NIC MACs must have been read.

        Returns:
            AvailableServerResult | None: The server holding the full lock, or `None` when it
                was taken by another request or failed (recorded in `tried`).
        """
        tried.ids.add(candidate.id)
        lease = self._probe_lease(candidate)
        deadline = max(1.0, lease.total_seconds() - _PROBE_LEASE_SLACK_SECONDS / 2)
        kept = False
        lost = False
        started = time.monotonic()
        marks: dict[str, float] = {}
        try:
            locked = await self._reservations.try_claim(candidate, claim, lease=lease)
            marks["claim"] = time.monotonic()
            if locked is None:
                lost = True
                tried.races += 1
                tried.failures.append(
                    (candidate.name, "RESERVED", "another request locked it first")
                )
                logger.info("available.claim_lost", server=candidate.name)
                return None
            tried.attempts += 1
            try:
                async with asyncio.timeout(deadline):
                    rechecked, live = await self._recheck(locked)
                marks["recheck"] = time.monotonic()
            except VendorRecheckError as exc:
                tried.failures.append((candidate.name, "VENDOR_ERROR", exc.reason))
                return None
            except TimeoutError:
                tried.failures.append(
                    (candidate.name, "VENDOR_ERROR", f"the live check ran over {deadline:.0f} s")
                )
                return None
            except ConflictError:
                tried.failures.append(
                    (candidate.name, "VENDOR_ERROR", "its record kept changing during the check")
                )
                return None
            if rechecked is None:
                tried.failures.append(
                    (candidate.name, "GONE_FROM_MANAGER", "its manager can no longer find it")
                )
                return None
            if not server_still_qualifies(
                rechecked, tiers=tiers, min_nic_macs=min_nic_macs, own_claim=claim
            ):
                reasons = unassignable_reasons(
                    rechecked, tiers=tiers, min_nic_macs=min_nic_macs, own_claim=claim
                )
                tried.failures.append(
                    (
                        candidate.name,
                        reasons[0][0] if reasons else "REJECTED",
                        "; ".join(text for _code, text in reasons) or "it no longer qualifies",
                    )
                )
                logger.info(
                    "available.candidate_rejected",
                    server=candidate.name,
                    health=rechecked.health.overall.value,
                    gone_from_manager=False,
                )
                return None
            final = await self._reservations.extend_claim(
                locked.id,
                claim,
                actor=actor,
                request_id=request_id,
                still_ok=lambda fresh: server_still_qualifies(
                    fresh, tiers=tiers, min_nic_macs=min_nic_macs, own_claim=claim
                ),
            )
            if final is None:
                tried.failures.append(
                    (candidate.name, "RESERVE_FAILED", "its lock could not be extended")
                )
                return None
            marks["extend"] = time.monotonic()
            kept = True
            return AvailableServerResult(server=final, live_recheck_performed=live)
        finally:
            ended = time.monotonic()
            logger.info(
                "available.candidate_timing",
                server=candidate.name,
                source=candidate.source_provider,
                kept=kept,
                claim_lost=lost,
                total_ms=round((ended - started) * 1000),
                claim_ms=round((marks.get("claim", ended) - started) * 1000),
                recheck_ms=round((marks["recheck"] - marks["claim"]) * 1000)
                if "recheck" in marks
                else None,
                extend_ms=round((marks["extend"] - marks["recheck"]) * 1000)
                if "extend" in marks
                else None,
            )
            if not kept and not lost:
                freed = await asyncio.shield(self._reservations.release_claim(candidate.id, claim))
                if not freed:
                    tried.stuck = True
                    tried.failures.append(
                        (candidate.name, "RELEASE_FAILED", "its lock could not be released")
                    )

    @staticmethod
    def _exhausted(subject: str, tried: _Tried) -> AvailableServersNotFoundError:
        """
        The error for a `POST` that locked and returned nothing.

        Args:
            subject (str): The quoted name or pattern.
            tried (_Tried): What the request did.

        Returns:
            AvailableServersNotFoundError: Names each candidate's failure, or says every match was
                taken by another request.
        """
        if not tried.failures:
            return AvailableServersNotFoundError(
                f"every server matching {subject} was taken by another request",
                ("RESERVED",),
            )
        listed = "; ".join(f"{name}: {text}" for name, _code, text in tried.failures)
        codes = tuple(dict.fromkeys(code for _name, code, _text in tried.failures))
        return AvailableServersNotFoundError(
            f"no server matching {subject} could be reserved "
            f"({tried.attempts} live-checked, {tried.races} taken by others): {listed}",
            codes,
        )

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

    async def _recheck(self, server: Server) -> tuple[Server | None, bool]:
        """
        Live-verify one server, persisting the fresh state through `IngestService`.

        Degrades to trusting `server` unchanged per ADR-0032, Decision 5.

        Args:
            server (Server): The stored candidate.

        Returns:
            tuple[Server | None, bool]: The current server (fresh if
                rechecked, `server` itself if the recheck was skipped or the
                configured manager of its type is not the one that collects it,
                or `None` if the manager can no longer find it at all), and
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
        fetched_at = started
        outcome = "failed"
        try:
            try:
                fresh = await provider.get_one(identity)
                fetched_at = time.monotonic()
            except NotThisManagersServerError:
                outcome = "skipped_foreign"
                return server, False
            except Exception as exc:
                logger.exception(
                    "available.recheck_failed", server=server.name, manager_type=manager_type.value
                )
                raise VendorRecheckError(
                    manager_type, server.name, recheck_failure_reason(exc)
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
                get_one_ms=round((fetched_at - started) * 1000),
                ingest_ms=round((started + elapsed - fetched_at) * 1000),
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
