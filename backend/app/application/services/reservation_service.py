"""`ReservationService`: take and release a server's install lock.

THE MUTUAL EXCLUSION IS `upsert_with_revision_check`, not a read-then-write.
Two callers racing for one machine both read the same `revision`, both build
their reservation from it, and both try to store it expecting that revision.
Mongo lets exactly one through; the other gets `RevisionConflictError` and is
told the server is taken. Checking `is_live()` first is only an optimisation —
it answers the common case without a write — and can never be the guarantee,
because anything read before a write is already stale by the time it is used.

WHAT IT REFUSES. A live reservation held by ANOTHER holder, which is the whole
point. A repeat call from the SAME workflow is not a refusal but an extension:
Temporal retries activities, so a claim that is not idempotent for its own
holder would make a retried claim look like a lost race and send the run off to
a different machine it does not need.

Like `MaintenanceService`, this touches only its own sub-document, `revision`
and `updated_at` — never classification, health or lifecycle state — which is
what keeps a reserved server free to be simultaneously HOSTED_CLUSTER, CRITICAL
and in maintenance without any of these services stepping on each other.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.application.services.audit_service import AuditService
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.reservation import Reservation
from app.domain.models.server import Server
from app.domain.ports.repository import ServerRepository
from app.errors import ConflictError, NotFoundError, RevisionConflictError
from app.utils.timeutil import utcnow


class ReservationService:
    """Takes and releases the install lock on a server."""

    def __init__(self, *, server_repo: ServerRepository, audit: AuditService) -> None:
        """
        Initialize the service with its server repository and audit service.

        Args:
            server_repo (ServerRepository): The servers collection.
            audit (AuditService): Records reserve/release events.
        """
        self._server_repo = server_repo
        self._audit = audit

    async def reserve(
        self,
        server_id: str,
        *,
        holder: str,
        mce_cluster: str,
        infra_env: str | None,
        namespace: str | None,
        workflow_id: str | None,
        ttl: timedelta,
        actor: Actor,
        request_id: str | None,
    ) -> Server:
        """
        Take the install lock on one server, or refuse because someone holds it.

        Args:
            server_id (str): The server to lock.
            holder (str): What is taking it, e.g. "install-server".
            mce_cluster (str): The MCE cluster the server is being installed
                into — recorded so the fleet list can say where it went.
            infra_env (str | None): The InfraEnv being filled.
            namespace (str | None): Where the BareMetalHost will be created.
            workflow_id (str | None): The run taking it, for tracing back.
            ttl (timedelta): How long the lock is honoured for. Bounded so a
                crashed run costs one window rather than the machine.
            actor (Actor): Who is reserving.
            request_id (str | None): The originating API request id, if any.

        Returns:
            Server: The server, now holding this reservation.

        Raises:
            NotFoundError: No server exists with `server_id`.
            ConflictError: A live reservation is held by a different holder, or
                another caller won the same race.
        """
        server = await self._get_or_404(server_id)
        existing = server.reservation
        now = utcnow()

        # A different holder's LIVE lock is the refusal this exists for. The
        # same workflow re-claiming is an extension, because Temporal retries.
        if existing.is_live(now=now) and not self._is_same_claim(
            existing, holder=holder, workflow_id=workflow_id
        ):
            await self._audit.record(
                EventType.SERVER_RESERVATION_REFUSED,
                actor=actor,
                server_id=server_id,
                request_id=request_id,
                data={
                    "requested_by": holder,
                    "requested_for_mce": mce_cluster,
                    "held_by": existing.holder,
                    "held_for_mce": existing.mce_cluster,
                    "expires_at": _iso(existing.expires_at),
                },
            )
            raise ConflictError(
                f"Server {server_id!r} is reserved by {existing.holder!r} for MCE "
                f"{existing.mce_cluster!r} until {_iso(existing.expires_at)}.",
                details={
                    "server_id": server_id,
                    "held_by": existing.holder,
                    "held_for_mce": existing.mce_cluster,
                    "held_for_infra_env": existing.infra_env,
                    "workflow_id": existing.workflow_id,
                    "expires_at": _iso(existing.expires_at),
                },
            )

        expected_revision = server.revision
        server.reservation = Reservation(
            holder=holder,
            mce_cluster=mce_cluster,
            infra_env=infra_env,
            namespace=namespace,
            workflow_id=workflow_id,
            # Preserved across an extension so the record still says when this
            # machine was first taken, not when it was last renewed.
            created_at=existing.created_at if existing.is_live(now=now) else now,
            expires_at=now + ttl,
        )
        server.revision += 1
        server.updated_at = now
        try:
            await self._server_repo.upsert_with_revision_check(
                server, expected_revision=expected_revision
            )
        except RevisionConflictError as exc:
            # THE RACE, resolved by the store rather than by us: another
            # caller wrote between our read and our write, so this document is
            # too stale to claim from — ADR-0035, decision 4.
            await self._audit.record(
                EventType.SERVER_RESERVATION_REFUSED,
                actor=actor,
                server_id=server_id,
                request_id=request_id,
                data={
                    "requested_by": holder,
                    "requested_for_mce": mce_cluster,
                    "reason": "lost the revision race",
                },
            )
            raise ConflictError(
                f"Server {server_id!r} was modified by another caller while being "
                f"reserved; it may now be held elsewhere.",
                details={"server_id": server_id, "requested_by": holder},
            ) from exc

        await self._audit.record(
            EventType.SERVER_RESERVED,
            actor=actor,
            server_id=server_id,
            request_id=request_id,
            data={
                "holder": holder,
                "mce_cluster": mce_cluster,
                "infra_env": infra_env,
                "workflow_id": workflow_id,
                "expires_at": _iso(server.reservation.expires_at),
            },
        )
        return server

    async def release(
        self,
        server_id: str,
        *,
        holder: str | None,
        workflow_id: str | None,
        actor: Actor,
        request_id: str | None,
    ) -> Server:
        """
        Release the install lock, returning the server to the pool.

        Releasing an unheld server is SUCCESS, so a retried release cannot fail.

        Args:
            server_id (str): The server to release.
            holder (str | None): The holder releasing it. When given, a lock
                held by someone else is refused rather than stolen.
            workflow_id (str | None): The run releasing it, if any.
            actor (Actor): Who is releasing.
            request_id (str | None): The originating API request id, if any.

        Returns:
            Server: The server, with the reservation cleared.

        Raises:
            NotFoundError: No server exists with `server_id`.
            ConflictError: A live reservation belongs to a different holder.
        """
        server = await self._get_or_404(server_id)
        existing = server.reservation
        now = utcnow()

        if not existing.is_live(now=now):
            return server

        if holder is not None and not self._is_same_claim(
            existing, holder=holder, workflow_id=workflow_id
        ):
            raise ConflictError(
                f"Server {server_id!r} is reserved by {existing.holder!r} for MCE "
                f"{existing.mce_cluster!r}, not by {holder!r}.",
                details={
                    "server_id": server_id,
                    "held_by": existing.holder,
                    "held_for_mce": existing.mce_cluster,
                },
            )

        expected_revision = server.revision
        # Cleared rather than expired-in-place: a release is a statement that
        # the machine is free NOW, and leaving a spent lock behind would keep
        # the fleet list claiming an install is in progress.
        server.reservation = Reservation()
        server.revision += 1
        server.updated_at = now
        try:
            await self._server_repo.upsert_with_revision_check(
                server, expected_revision=expected_revision
            )
        except RevisionConflictError as exc:
            raise ConflictError(
                f"Server {server_id!r} was modified by another caller while being released.",
                details={"server_id": server_id},
            ) from exc

        await self._audit.record(
            EventType.SERVER_RELEASED,
            actor=actor,
            server_id=server_id,
            request_id=request_id,
            data={
                "holder": existing.holder,
                "mce_cluster": existing.mce_cluster,
                "workflow_id": existing.workflow_id,
            },
        )
        return server

    @staticmethod
    def _is_same_claim(existing: Reservation, *, holder: str, workflow_id: str | None) -> bool:
        """Whether a claim is the SAME one, so re-taking it is an extension.

        Keyed on the workflow id, not the holder — ADR-0035, decision 5. Falls
        back to the holder only when neither side names a run.
        """
        if existing.holder != holder:
            return False
        if workflow_id is None and existing.workflow_id is None:
            return True
        return existing.workflow_id == workflow_id

    async def _get_or_404(self, server_id: str) -> Server:
        server = await self._server_repo.get_by_id(server_id)
        if server is None:
            raise NotFoundError(
                f"No server with id {server_id!r}.", details={"server_id": server_id}
            )
        return server


def _iso(value: datetime | None) -> str | None:
    """An instant as ISO-8601 for a message or an audit payload."""
    return None if value is None else value.isoformat()
