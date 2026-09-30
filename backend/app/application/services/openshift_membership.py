"""Reconcile one cluster's view of which servers it holds.

This service only ever touches `Server.openshift`, `revision` and
`updated_at` — the same discipline `MaintenanceService` follows, and the
reason a server can be simultaneously `HOSTED_CLUSTER`, `CRITICAL`, in
maintenance and `INSTALLED` without four writers fighting over one
document.

Deliberately not `IngestService`. That service rebuilds a whole `Server`
from a `ProviderServer` and replaces the document; routing cluster
membership through it would blank `name`, `identity.external_ids`,
`network.interfaces` and `connectivity`, none of which a cluster knows
anything about.

**Why this reconciles a set rather than writing what it saw.** Nothing in
Kubernetes reports a *removal*: a server freed from a cluster simply stops
appearing in its node list. A job that only wrote its observations would
leave every server it ever saw marked `INSTALLED` forever. So each run
also frees the servers that still name this cluster but were not seen this
time — and only those, which is what makes it safe for a job that can see
one cluster to run alongside jobs that see others.

**The hardware-serial fallback (ADR-0036).** A vendor-side rename changes
`Server.name` but not the hardware, so a hostname match can miss a server
that is still very much installed — or, when two servers happen to share a
name, land on the wrong one. For a **node**, whose serial costs an SSH
round trip (`SerialReader`), this only runs when the hostname is not a
*unique* match (a miss, or 2+ servers sharing it) — bounding the fan-out.
An **agent** already carries its serial for free on the same CR read
(`records.agent_observation`), so it is resolved by serial *first,
always* — even when the hostname looks unique, closing half of the "known
gap" below for the one source verifying costs nothing (2026-09-28,
`_resolve_agent`). Its `matched_by_serial` only counts a genuine name
disagreement (`_name_mismatch`), not every match — unlike a node, where
reaching the fallback at all already means the hostname missed or was a
duplicate, an agent resolves by serial routinely, so counting every one
would track ordinary traffic instead of an anomaly. Either path matches on
`identity.serial_normalized`. A resolved match keeps the server
`INSTALLED` and records what the cluster actually called it
(`OpenShiftLifecycle.reported_name`) rather than freeing it as absent.

**When a serial cannot be read, protection is scoped to what is actually
ambiguous, not the whole run (2026-09-27 correction).** An earlier version
of this fallback skipped the *entire* cluster's free-on-absence pass on any
single unreadable serial — one permanently unreachable node then blocked
every other server's release forever, which is itself a correctness
failure the same way a stale claim is. A duplicate name's candidates are
known (`_find_by_hostname` already found them); an unreadable serial for
one leaves *only those specific candidates* unfreeable this run, while
every other server in scope is freed normally. A plain miss (zero
candidates) has no specific server to protect — there is nothing to
narrow the risk to, and indefinitely freezing the whole cluster to guard
an unidentifiable single machine is a worse trade than accepting that
narrow, already-rare residual risk. With no `SerialReader` configured at
all, a duplicate hostname keeps the arbitrary first-by-name pick this
service always made, so a cluster with no key mounted sees no behaviour
change from this feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import structlog

from app.application.services.audit_service import AuditService
from app.domain.enums import OpenShiftState
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Server
from app.domain.ports.repository import ServerRepository
from app.domain.services.normalize import normalize_text
from app.infrastructure.openshift.records import ClusterObservation
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

_PAGE = 500


class SerialReader(Protocol):
    """What `OpenShiftMembershipService` needs to resolve a node's hardware serial (ADR-0036)."""

    async def read(self, address: str) -> str | None:
        """
        Read one node's hardware serial.

        Args:
            address (str): The node's `InternalIP`.

        Returns:
            str | None: The raw serial, or `None` if it could not be read.
        """
        ...


@dataclass(slots=True)
class MembershipSummary:
    """
    What one reconcile run did.

    Attributes:
        observed (int): Observations the cluster reported.
        matched (int): Observations that resolved to a known server.
        claimed (int): Servers newly claimed or updated by this run.
        freed (int): Servers released because this cluster no longer
            lists them.
        unmatched (list[str]): Hostnames the cluster reported that no
            server matched, by name or by serial. Never created — the
            vendor collectors are the only source of what hardware exists.
        matched_by_serial (int): Of `matched`, resolved via the hardware
            serial fallback rather than a unique hostname match
            (ADR-0036) — a renamed or duplicate-named server.
        unresolved (list[str]): Hostnames that missed by name and whose
            hardware serial could not be read this run — a subset of
            `unmatched`. For a duplicate name, its specific candidates are
            held back from freeing this run (2026-09-27 correction); a
            plain miss protects nothing else, since there is no candidate
            to narrow the risk to.
    """

    observed: int = 0
    matched: int = 0
    claimed: int = 0
    freed: int = 0
    unmatched: list[str] = field(default_factory=list)
    matched_by_serial: int = 0
    unresolved: list[str] = field(default_factory=list)


class OpenShiftMembershipService:
    """Applies one cluster's observations to the inventory."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        audit: AuditService,
        actor: Actor,
        serial_reader: SerialReader | None = None,
    ) -> None:
        """
        Build the service.

        Args:
            server_repo (ServerRepository): The servers collection.
            audit (AuditService): Records state transitions.
            actor (Actor): The job identity written onto audit events.
            serial_reader (SerialReader | None): Resolves a node's hardware
                serial over SSH (ADR-0036), for `--source nodes` only.
                `None` disables the fallback entirely — a hostname miss or
                duplicate then behaves exactly as it did before this
                feature existed.
        """
        self._server_repo = server_repo
        self._audit = audit
        self._actor = actor
        self._serial_reader = serial_reader

    async def reconcile(
        self,
        observations: list[ClusterObservation],
        *,
        scope: dict[str, object],
        reported_by: str,
        dry_run: bool = False,
    ) -> MembershipSummary:
        """
        Apply what one cluster reported, and free what it no longer holds.

        Args:
            observations (list[ClusterObservation]): Everything this
                cluster reported this run.
            scope (dict[str, object]): The Mongo filter identifying the
                servers this job owns — `openshift.cluster_name` for a
                nodes job, `openshift.mce_name` for an agents job. Only
                servers inside it may be freed, which is what stops one
                cluster's job releasing another cluster's machines.
            reported_by (str): The cluster or MCE doing the reporting.
            dry_run (bool): Compute everything, write nothing.

        Returns:
            MembershipSummary: Counts and the unmatched hostnames.
        """
        summary = MembershipSummary(observed=len(observations))
        seen_ids: set[str] = set()
        protected_ids: set[str] = set()

        for observation in observations:
            server, reported_name = await self._resolve(
                observation, reported_by, summary, protected_ids
            )
            if server is None:
                continue
            summary.matched += 1
            seen_ids.add(server.id)
            if await self._apply(
                server, observation, reported_by, reported_name=reported_name, dry_run=dry_run
            ):
                summary.claimed += 1

        if protected_ids:
            # Deferred, not lost — see the module docstring's 2026-09-27
            # correction. Only these specific candidates are held back;
            # every other server in scope is still freed normally below.
            logger.warning(
                "openshift.free_partially_protected",
                reported_by=reported_by,
                unresolved=len(summary.unresolved),
                protected=len(protected_ids),
            )

        for server in await self._claimed_by(scope):
            if server.id in seen_ids or server.id in protected_ids:
                continue
            if await self._free(server, dry_run=dry_run):
                summary.freed += 1

        logger.info(
            "openshift.reconciled",
            reported_by=reported_by,
            observed=summary.observed,
            matched=summary.matched,
            matched_by_serial=summary.matched_by_serial,
            claimed=summary.claimed,
            freed=summary.freed,
            unmatched=len(summary.unmatched),
            unresolved=len(summary.unresolved),
            dry_run=dry_run,
        )
        return summary

    async def _resolve(
        self,
        observation: ClusterObservation,
        reported_by: str,
        summary: MembershipSummary,
        protected_ids: set[str],
    ) -> tuple[Server | None, str | None]:
        """
        Resolve one observation to a server, falling back to its hardware serial.

        Args:
            observation (ClusterObservation): What the cluster reported.
            reported_by (str): The reporting cluster or MCE, for logging.
            summary (MembershipSummary): Updated in place with `unmatched`/
                `unresolved` on a miss, and `matched_by_serial` on a serial
                match — the caller still owns `matched`/`claimed`.
            protected_ids (set[str]): Updated in place with a duplicate's
                candidate ids when its serial cannot be read — the only
                servers a failed read exempts from this run's freeing.

        Returns:
            tuple[Server | None, str | None]: The matched server (or
                `None`), and the `reported_name` to record — the
                observation's hostname when it differs from the matched
                server's own name, else `None`.
        """
        if observation.serial:
            return await self._resolve_agent(observation, observation.serial, reported_by, summary)

        candidates = await self._find_by_hostname(observation.hostname)
        if len(candidates) == 1:
            return candidates[0], None

        attempted, serial = await self._resolve_serial(observation)

        if not attempted:
            # No serial capability at all — see the module docstring.
            if candidates:
                logger.warning(
                    "openshift.duplicate_hostname",
                    hostname=observation.hostname,
                    reported_by=reported_by,
                    count=len(candidates),
                )
                return candidates[0], None
            summary.unmatched.append(observation.hostname)
            logger.error(
                "openshift.host_not_in_inventory",
                hostname=observation.hostname,
                reported_by=reported_by,
            )
            return None, None

        if serial is None:
            summary.unmatched.append(observation.hostname)
            summary.unresolved.append(observation.hostname)
            # A miss has no candidate to protect — there is nothing to
            # narrow the risk to, so nothing else in scope is held back.
            protected_ids.update(candidate.id for candidate in candidates)
            logger.error(
                "openshift.serial_unreadable",
                hostname=observation.hostname,
                reported_by=reported_by,
                duplicate=bool(candidates),
                protected=len(candidates),
            )
            return None, None

        server = await self._find_by_serial(serial)
        if server is None:
            summary.unmatched.append(observation.hostname)
            logger.error(
                "openshift.host_not_in_inventory",
                hostname=observation.hostname,
                reported_by=reported_by,
                serial_checked=True,
            )
            return None, None

        summary.matched_by_serial += 1
        reported_name = (
            observation.hostname if observation.hostname != server.name_normalized else None
        )
        if reported_name:
            logger.warning(
                "openshift.name_mismatch",
                hostname=observation.hostname,
                server_name=server.name,
                reported_by=reported_by,
            )
        return server, reported_name

    async def _resolve_agent(
        self,
        observation: ClusterObservation,
        serial: str,
        reported_by: str,
        summary: MembershipSummary,
    ) -> tuple[Server | None, str | None]:
        """
        Resolve an Agent by its own reported serial, always — see the module docstring.

        Args:
            observation (ClusterObservation): What the MCE reported.
            serial (str): `observation.serial`, already known non-empty.
            reported_by (str): The reporting MCE, for logging.
            summary (MembershipSummary): Updated in place on a miss.

        Returns:
            tuple[Server | None, str | None]: The matched server (or
                `None`), and the `reported_name` to record.
        """
        server = await self._find_by_serial(serial)
        if server is not None:
            return server, self._name_mismatch(observation, server, reported_by, summary)

        # This exact serial matches nothing yet (freshly reported, or an
        # ambiguous cross-vendor collision) — a *unique* hostname is still
        # a safe fallback; a miss or a duplicate is not guessed at.
        candidates = await self._find_by_hostname(observation.hostname)
        if len(candidates) == 1:
            return candidates[0], None
        summary.unmatched.append(observation.hostname)
        logger.error(
            "openshift.host_not_in_inventory",
            hostname=observation.hostname,
            reported_by=reported_by,
            serial_checked=True,
        )
        return None, None

    def _name_mismatch(
        self,
        observation: ClusterObservation,
        server: Server,
        reported_by: str,
        summary: MembershipSummary,
    ) -> str | None:
        """
        The `reported_name` for an agent's serial match — see the module docstring.

        Args:
            observation (ClusterObservation): What the cluster reported.
            server (Server): The server the serial resolved to.
            reported_by (str): The reporting MCE, for logging.
            summary (MembershipSummary): `matched_by_serial` incremented
                only when the names actually disagree (unlike the node
                path, where reaching it at all is already the anomaly).

        Returns:
            str | None: The observation's hostname, when it differs from
                `server`'s own name; else `None`.
        """
        if observation.hostname == server.name_normalized:
            return None
        summary.matched_by_serial += 1
        logger.warning(
            "openshift.name_mismatch",
            hostname=observation.hostname,
            server_name=server.name,
            reported_by=reported_by,
        )
        return observation.hostname

    async def _resolve_serial(self, observation: ClusterObservation) -> tuple[bool, str | None]:
        """
        Read the hardware serial an observation names, if this service can.

        Args:
            observation (ClusterObservation): What the cluster reported.

        Returns:
            tuple[bool, str | None]: Whether a read was attempted at all,
                and the serial if one was read. `(False, None)` means no
                capability existed (no reader, no serial on the
                observation) — distinct from `(True, None)`, an attempted
                read that failed.
        """
        if observation.serial:
            return True, observation.serial
        if self._serial_reader is not None and observation.address:
            return True, await self._serial_reader.read(observation.address)
        return False, None

    async def _find_by_hostname(self, hostname: str) -> list[Server]:
        """
        Every server a reported hostname names.

        Matches `name_normalized`, not a serial — that's `_resolve_serial`'s job.

        Args:
            hostname (str): A cleaned hostname.

        Returns:
            list[Server]: Up to two matches — enough to tell "exactly one"
                from "a duplicate name" without paging the whole set.
        """
        page = await self._server_repo.list_page(
            filters={"name_normalized": hostname},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=2,
            with_count=False,
        )
        return list(page.items)

    async def _find_by_serial(self, serial: str) -> Server | None:
        """
        The one server a hardware serial names, if exactly one does.

        Args:
            serial (str): A raw serial, as read from the machine.

        Returns:
            Server | None: The match, or `None` if no server carries it or
                more than one does (logged as ambiguous — uniqueness on
                `identity.serial_normalized` is enforced only per vendor).
        """
        serial_normalized = normalize_text(serial)
        if not serial_normalized:
            return None
        page = await self._server_repo.list_page(
            filters={"identity.serial_normalized": serial_normalized},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=2,
            with_count=False,
        )
        if len(page.items) > 1:
            logger.warning(
                "openshift.serial_ambiguous",
                serial=serial_normalized,
                count=len(page.items),
            )
            return None
        return page.items[0] if page.items else None

    async def _claimed_by(self, scope: dict[str, object]) -> list[Server]:
        """
        Every server currently naming this cluster.

        Args:
            scope (dict[str, object]): The ownership filter.

        Returns:
            list[Server]: All matching servers, across every page.
        """
        found: list[Server] = []
        cursor: str | None = None
        while True:
            page = await self._server_repo.list_page(
                filters=dict(scope),
                search=None,
                sort="name",
                sort_desc=False,
                cursor=cursor,
                page_size=_PAGE,
                with_count=False,
            )
            found.extend(page.items)
            if not page.has_more or page.next_cursor is None:
                return found
            cursor = page.next_cursor

    async def _apply(
        self,
        server: Server,
        observation: ClusterObservation,
        reported_by: str,
        *,
        reported_name: str | None,
        dry_run: bool,
    ) -> bool:
        """
        Write one observation onto a server, if it changes anything.

        Args:
            server (Server): The matched server.
            observation (ClusterObservation): What the cluster reported.
            reported_by (str): The reporting cluster or MCE.
            reported_name (str | None): The cluster's hostname for this
                server, when it differs from `server.name` (ADR-0036).
            dry_run (bool): Compute only.

        Returns:
            bool: Whether the stored value changed.
        """
        updated = OpenShiftLifecycle(
            lifecycle_state=observation.lifecycle_state,
            cluster_name=observation.cluster_name,
            mce_name=observation.mce_name,
            last_reported_at=utcnow(),
            reported_by_agent_id=reported_by,
            reported_name=reported_name,
        )
        return await self._write(server, updated, dry_run=dry_run)

    async def _free(self, server: Server, *, dry_run: bool) -> bool:
        """
        Release a server this cluster no longer lists.

        Args:
            server (Server): A server still naming this cluster.
            dry_run (bool): Compute only.

        Returns:
            bool: Whether the stored value changed.
        """
        return await self._write(
            server,
            OpenShiftLifecycle(lifecycle_state=OpenShiftState.AVAILABLE),
            dry_run=dry_run,
        )

    async def _write(self, server: Server, updated: OpenShiftLifecycle, *, dry_run: bool) -> bool:
        """
        Persist a new membership, skipping a write that changes nothing.

        Writing unconditionally would bump every server's `revision` four
        times an hour and fill the audit trail with non-events.

        Args:
            server (Server): The server to update.
            updated (OpenShiftLifecycle): The new membership.
            dry_run (bool): Compute only.

        Returns:
            bool: Whether anything changed.
        """
        before = server.openshift
        if (
            before.lifecycle_state is updated.lifecycle_state
            and before.cluster_name == updated.cluster_name
            and before.mce_name == updated.mce_name
            and before.reported_name == updated.reported_name
        ):
            return False
        if dry_run:
            return True

        expected_revision = server.revision
        server.openshift = updated
        server.revision += 1
        server.updated_at = utcnow()
        await self._server_repo.upsert_with_revision_check(
            server, expected_revision=expected_revision
        )

        if before.lifecycle_state is not updated.lifecycle_state:
            await self._audit.record(
                EventType.OPENSHIFT_STATE_CHANGED,
                actor=self._actor,
                server_id=server.id,
                server_name=server.name,
                request_id=None,
                data={
                    "from": before.lifecycle_state.value,
                    "to": updated.lifecycle_state.value,
                    "cluster_name": updated.cluster_name,
                },
            )
        return True
