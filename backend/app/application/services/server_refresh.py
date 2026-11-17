"""`POST /servers/{id}/refresh`: read one server from its vendor manager now (ADR-0062).

The same `get_one()` + `IngestService.ingest_one` a `/servers/available` live recheck runs,
but every case a recheck degrades on silently is an error the caller sees.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.application.services.audit_service import AuditService
from app.application.services.available_servers import fetch_live
from app.application.services.ingest import IngestService
from app.domain.enums import ManagerType
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.server import Server
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.ports.provider import NotThisManagersServerError, ServerInventoryProvider
from app.domain.ports.repository import ServerRepository
from app.errors import NotFoundError, ServerNotAtManagerError, ServerRefreshUnsupportedError


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one refresh did.

    Attributes:
        server (Server): The server as persisted after the read.
        changed (bool): Whether its content changed (a new `revision`), not merely re-confirmed.
    """

    server: Server
    changed: bool


class ServerRefreshService:
    """Reads one server from the manager that collects it and persists the result."""

    def __init__(
        self,
        *,
        repo: ServerRepository,
        ingest: IngestService,
        provider_factory: Callable[[ManagerType], ServerInventoryProvider],
        audit: AuditService,
    ) -> None:
        """
        Build the service for one request.

        Args:
            repo (ServerRepository): The servers collection.
            ingest (IngestService): The pipeline a collection run writes through.
            provider_factory (Callable[[ManagerType], ServerInventoryProvider]): Builds one
                collector; raises `ManagerNotConfiguredError` when this process has no connection.
            audit (AuditService): Records `SERVER_REFRESHED`.
        """
        self._repo = repo
        self._ingest = ingest
        self._provider_factory = provider_factory
        self._audit = audit

    async def refresh(
        self, server_id: str, *, actor: Actor, request_id: str | None
    ) -> RefreshOutcome:
        """
        Read `server_id` from its manager, persist it, and record who asked.

        Args:
            server_id (str): The server's ID.
            actor (Actor): The admin who asked.
            request_id (str | None): The API request id, for the audit event.

        Returns:
            RefreshOutcome: The persisted server and whether its content changed.

        Raises:
            NotFoundError: No server has that ID.
            ServerRefreshUnsupportedError: No collector, no connection on this process, or the
                configured manager of its type does not collect it.
            ServerNotAtManagerError: The manager no longer reports it; nothing was written.
            VendorRecheckError: The manager failed (timeout, login, network).
        """
        server = await self._repo.get_by_id(server_id)
        if server is None:
            raise NotFoundError(
                f"No server with id {server_id!r}.", details={"server_id": server_id}
            )
        if server.source_provider is None:
            raise ServerRefreshUnsupportedError(
                f"{server.name!r} was not collected by a vendor manager, so there is nothing to "
                "read it from.",
                details={"server_id": server_id},
            )
        manager_type = ManagerType(server.source_provider)
        provider = self._provider(manager_type, server)
        try:
            fresh = await fetch_live(
                server,
                manager_type=manager_type,
                provider=provider,
                ingest=self._ingest,
                log_event="server.refresh",
            )
        except NotThisManagersServerError as exc:
            raise ServerRefreshUnsupportedError(
                f"The {manager_type.value} manager this API is connected to does not collect "
                f"{server.name!r}; its own collector job reads it.",
                details={"server_id": server_id, "manager_type": manager_type.value},
            ) from exc
        if fresh is None:
            raise ServerNotAtManagerError(
                f"The {manager_type.value} manager no longer reports {server.name!r}; "
                "the stored data is unchanged.",
                details={"server_id": server_id, "manager_type": manager_type.value},
            )
        changed = fresh.revision != server.revision
        await self._audit.record(
            EventType.SERVER_REFRESHED,
            actor=actor,
            server_id=fresh.id,
            server_name=fresh.name,
            request_id=request_id,
            data={"manager_type": manager_type.value, "changed": changed},
        )
        return RefreshOutcome(server=fresh, changed=changed)

    def _provider(self, manager_type: ManagerType, server: Server) -> ServerInventoryProvider:
        """
        Build the collector for `manager_type`, or say why this process cannot.

        Args:
            manager_type (ManagerType): The server's collector.
            server (Server): The server, for the error message.

        Returns:
            ServerInventoryProvider: The collector.

        Raises:
            ServerRefreshUnsupportedError: No connection for it is configured on this process.
        """
        try:
            return self._provider_factory(manager_type)
        except (ManagerNotConfiguredError, NotImplementedError) as exc:
            raise ServerRefreshUnsupportedError(
                f"The API is not connected to the {manager_type.value} manager that collects "
                f"{server.name!r} ({exc}); run its collector job instead.",
                details={"server_id": server.id, "manager_type": manager_type.value},
            ) from exc
