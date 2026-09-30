"""`MaintenanceService`: enable/disable a server's maintenance window.

Maintenance is deliberately independent of classification and health (see
`app.domain.models.maintenance.Maintenance`'s docstring) — this service
only ever touches `Server.maintenance`, `revision`, and `updated_at`. It
never reads or writes `classification`/`health`, which is what lets a
server be simultaneously HOSTED_CLUSTER, CRITICAL, and in maintenance
without any of those three services stepping on each other's fields.
"""

from __future__ import annotations

from datetime import datetime

from app.application.services.audit_service import AuditService
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.maintenance import Maintenance
from app.domain.models.server import Server
from app.domain.ports.repository import ServerRepository
from app.errors import NotFoundError
from app.utils.timeutil import utcnow


class MaintenanceService:
    """Enables and disables a server's maintenance window."""

    def __init__(self, *, server_repo: ServerRepository, audit: AuditService) -> None:
        """
        Initialize the service with its server repository and audit service.

        Args:
            server_repo (ServerRepository): The servers collection.
            audit (AuditService): Records maintenance enable/disable events.
        """
        self._server_repo = server_repo
        self._audit = audit

    async def enable(
        self,
        server_id: str,
        *,
        reason: str | None,
        expected_end: datetime | None,
        actor: Actor,
        request_id: str | None,
    ) -> Server:
        """
        Enable (or update) a server's maintenance window.

        Args:
            server_id (str): The server to put into maintenance.
            reason (str | None): Why the server is in maintenance.
            expected_end (datetime | None): When the window is expected to end.
            actor (Actor): Who is enabling maintenance.
            request_id (str | None): The originating API request id, if any.

        Returns:
            Server: The updated server, with `maintenance` and `revision` bumped.

        Raises:
            NotFoundError: No server exists with `server_id`.
        """
        server = await self._get_or_404(server_id)
        expected_revision = server.revision
        was_enabled = server.maintenance.enabled
        now = utcnow()

        server.maintenance = Maintenance(
            enabled=True,
            reason=reason,
            created_by=actor.id if not was_enabled else server.maintenance.created_by,
            created_at=server.maintenance.created_at if was_enabled else now,
            updated_at=now,
            expected_end=expected_end,
        )
        server.revision += 1
        server.updated_at = now
        await self._server_repo.upsert_with_revision_check(
            server, expected_revision=expected_revision
        )

        await self._audit.record(
            EventType.MAINTENANCE_UPDATED if was_enabled else EventType.MAINTENANCE_ENABLED,
            actor=actor,
            server_id=server_id,
            server_name=server.name,
            request_id=request_id,
            data={"reason": reason},
        )
        return server

    async def disable(self, server_id: str, *, actor: Actor, request_id: str | None) -> Server:
        """
        End a server's maintenance window.

        Args:
            server_id (str): The server to take out of maintenance.
            actor (Actor): Who is disabling maintenance.
            request_id (str | None): The originating API request id, if any.

        Returns:
            Server: The updated server, with `maintenance` and `revision` bumped.

        Raises:
            NotFoundError: No server exists with `server_id`.
        """
        server = await self._get_or_404(server_id)
        expected_revision = server.revision
        was_enabled = server.maintenance.enabled
        now = utcnow()

        server.maintenance = Maintenance(enabled=False, updated_at=now)
        server.revision += 1
        server.updated_at = now
        await self._server_repo.upsert_with_revision_check(
            server, expected_revision=expected_revision
        )

        if was_enabled:
            await self._audit.record(
                EventType.MAINTENANCE_DISABLED,
                actor=actor,
                server_id=server_id,
                server_name=server.name,
                request_id=request_id,
            )
        return server

    async def _get_or_404(self, server_id: str) -> Server:
        server = await self._server_repo.get_by_id(server_id)
        if server is None:
            raise NotFoundError(
                f"No server with id {server_id!r}.", details={"server_id": server_id}
            )
        return server
