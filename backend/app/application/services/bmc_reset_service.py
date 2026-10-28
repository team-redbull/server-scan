"""Decide, and audit, an iDRAC restart for a server with all-UNKNOWN disks or NICs (ADR-0050)."""

from __future__ import annotations

import structlog

from app.application.services.audit_service import SYSTEM_INGEST_ACTOR, AuditService
from app.domain.models.audit_event import EventType
from app.domain.models.server import Server
from app.domain.ports.bmc_reset import BmcResetPolicy, BmcResetter, ResetOutcome
from app.domain.services.bmc_reset import reset_trigger
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)


class BmcResetService:
    """Restarts a server's BMC at most once per cooldown, and records each attempt."""

    def __init__(
        self, *, resetter: BmcResetter, audit: AuditService, policy: BmcResetPolicy
    ) -> None:
        """
        Bind the resetter, the audit trail (which is also the cooldown ledger) and the limits.

        Args:
            resetter (BmcResetter): Talks to the BMC.
            audit (AuditService): Records `IDRAC_RESET` and answers "reset recently?".
            policy (BmcResetPolicy): Report-only switch, cooldown and per-run cap.
        """
        self._resetter = resetter
        self._audit = audit
        self._policy = policy

    @property
    def max_per_run(self) -> int:
        """
        The most BMCs one collector run may reset.

        Returns:
            int: The per-run cap; 0 means none.
        """
        return self._policy.max_per_run

    async def maybe_reset(self, server: Server) -> bool:
        """
        Reset the server's BMC if it qualifies, is out of cooldown and its job queue is idle.

        Never raises: a failure here must not fail the ingest of the server.

        Args:
            server (Server): The server as just persisted.

        Returns:
            bool: True when this used one of the run's resets (or report-only previews).
        """
        trigger = reset_trigger(server)
        if trigger is None:
            return False
        try:
            cooled_down = utcnow() - self._policy.cooldown
            if await self._audit.has_event_since(EventType.IDRAC_RESET, server.id, cooled_down):
                return False
            result = await self._resetter.reset(server, apply=not self._policy.report_only)
            fields = {
                "server_id": server.id,
                "server_name": server.name,
                "trigger": trigger,
                "outcome": result.outcome.value,
                "bmc_host": result.bmc_host,
                "detail": result.detail,
                "active_jobs": list(result.active_jobs),
                "powered_on": result.powered_on,
                "power_state": result.power_state,
            }
            if result.outcome in (ResetOutcome.WOULD_RESET, ResetOutcome.SKIPPED_ACTIVE_JOBS):
                logger.info("idrac_reset.not_sent", **fields)
                return result.outcome is ResetOutcome.WOULD_RESET
            logger.warning("idrac_reset.attempted", **fields)
            await self._audit.record(
                EventType.IDRAC_RESET,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                server_name=server.name,
                data={k: v for k, v in fields.items() if k not in ("server_id", "server_name")},
            )
        except Exception:
            logger.exception("idrac_reset.failed", server_id=server.id)
            return False
        return True
