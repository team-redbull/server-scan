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
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from app.application.services.audit_service import AuditService
from app.domain.enums import OpenShiftState
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Server
from app.domain.ports.repository import ServerRepository
from app.infrastructure.openshift.records import ClusterObservation
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

_PAGE = 500


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
            server matched. Never created — the vendor collectors are the
            only source of what hardware exists.
    """

    observed: int = 0
    matched: int = 0
    claimed: int = 0
    freed: int = 0
    unmatched: list[str] = field(default_factory=list)


class OpenShiftMembershipService:
    """Applies one cluster's observations to the inventory."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        audit: AuditService,
        actor: Actor,
    ) -> None:
        """
        Build the service.

        Args:
            server_repo (ServerRepository): The servers collection.
            audit (AuditService): Records state transitions.
            actor (Actor): The job identity written onto audit events.
        """
        self._server_repo = server_repo
        self._audit = audit
        self._actor = actor

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

        for observation in observations:
            server = await self._find_by_hostname(observation.hostname)
            if server is None:
                summary.unmatched.append(observation.hostname)
                logger.error(
                    "openshift.host_not_in_inventory",
                    hostname=observation.hostname,
                    reported_by=reported_by,
                )
                continue
            summary.matched += 1
            seen_ids.add(server.id)
            if await self._apply(server, observation, reported_by, dry_run=dry_run):
                summary.claimed += 1

        for server in await self._claimed_by(scope):
            if server.id in seen_ids:
                continue
            if await self._free(server, dry_run=dry_run):
                summary.freed += 1

        logger.info(
            "openshift.reconciled",
            reported_by=reported_by,
            observed=summary.observed,
            matched=summary.matched,
            claimed=summary.claimed,
            freed=summary.freed,
            unmatched=len(summary.unmatched),
            dry_run=dry_run,
        )
        return summary

    async def _find_by_hostname(self, hostname: str) -> Server | None:
        """
        The server a reported hostname names, if any.

        Matches `name_normalized`, not `IngestService`'s
        `(vendor, serial_normalized)` — a cluster knows no serials.

        Args:
            hostname (str): A cleaned hostname.

        Returns:
            Server | None: The match, or None.
        """
        page = await self._server_repo.list_page(
            filters={"name_normalized": hostname},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=1,
            with_count=False,
        )
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
        dry_run: bool,
    ) -> bool:
        """
        Write one observation onto a server, if it changes anything.

        Args:
            server (Server): The matched server.
            observation (ClusterObservation): What the cluster reported.
            reported_by (str): The reporting cluster or MCE.
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
                request_id=None,
                data={
                    "from": before.lifecycle_state.value,
                    "to": updated.lifecycle_state.value,
                    "cluster_name": updated.cluster_name,
                },
            )
        return True
