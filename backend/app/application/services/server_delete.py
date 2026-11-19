"""`ServerDeleteService`: remove one server from the database by hand (docs/adr/0065)."""

from __future__ import annotations

from app.application.services.audit_service import AuditService
from app.domain.enums import OpenShiftState
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.server import Server
from app.domain.ports.repository import ServerRepository
from app.errors import NotFoundError, ServerDeleteBlockedError


def delete_blockers(server: Server) -> list[str]:
    """
    Say why a server should not be deleted without `force`.

    Args:
        server (Server): The server about to be deleted.

    Returns:
        list[str]: One human-readable reason per blocker; empty when it is free to delete.
    """
    reasons: list[str] = []
    state = server.openshift.lifecycle_state
    if state != OpenShiftState.AVAILABLE:
        where = f" in {server.openshift.cluster_name}" if server.openshift.cluster_name else ""
        reasons.append(f"it is in use by OpenShift ({state.value}{where})")
    if server.reservation.is_live():
        holder = server.reservation.mce_cluster or server.reservation.holder or "an install"
        reasons.append(f"it is reserved for an install ({holder})")
    if server.maintenance.enabled:
        reasons.append("it is in maintenance")
    return reasons


class ServerDeleteService:
    """Deletes one server, refusing one that is in use unless forced, and audits it."""

    def __init__(self, *, repo: ServerRepository, audit: AuditService) -> None:
        """
        Build the service for one request.

        Args:
            repo (ServerRepository): The servers collection.
            audit (AuditService): Records `SERVER_DELETED`.
        """
        self._repo = repo
        self._audit = audit

    async def delete(
        self, server_id: str, *, force: bool, actor: Actor, request_id: str | None
    ) -> Server:
        """
        Delete a server, after recording who deleted it and what was overridden.

        Args:
            server_id (str): The server to delete.
            force (bool): Delete even though it is installed, reserved or in maintenance.
            actor (Actor): Who is deleting it.
            request_id (str | None): The originating API request id, if any.

        Returns:
            Server: The server as it was just before deletion.

        Raises:
            NotFoundError: No server has that ID.
            ServerDeleteBlockedError: It is in use and `force` is false.
        """
        server = await self._repo.get_by_id(server_id)
        if server is None:
            raise NotFoundError(f"Server {server_id} not found.")
        blockers = delete_blockers(server)
        if blockers and not force:
            raise ServerDeleteBlockedError(
                f"{server.name} is not deleted: {'; '.join(blockers)}. "
                "Use force to delete it anyway.",
                details={"blockers": blockers},
            )
        await self._audit.record(
            EventType.SERVER_DELETED,
            actor=actor,
            server_id=server.id,
            server_name=server.name,
            server=server,
            request_id=request_id,
            data={
                "vendor": server.identity.vendor.value,
                "source_provider": server.source_provider,
                "forced": bool(blockers),
                "overridden": blockers,
            },
        )
        if not await self._repo.delete(server_id):
            raise NotFoundError(f"Server {server_id} not found.")
        return server
