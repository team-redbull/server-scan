"""The BMC reset seam: ask one server's BMC to restart itself (ADR-0050)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from app.domain.models.server import Server


class ResetOutcome(StrEnum):
    """What one reset attempt did; closed, and recorded on the audit event."""

    REQUESTED = "requested"
    UNCONFIRMED = "unconfirmed"
    WOULD_RESET = "would_reset"
    SKIPPED_ACTIVE_JOBS = "skipped_active_jobs"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BmcResetPolicy:
    """
    How often and how many BMCs one collector run may reset.

    Attributes:
        report_only (bool): Read the job queue and log, never send the restart.
        cooldown (timedelta): A server with a reset event younger than this is left alone.
        max_per_run (int): Most BMCs one run may reset (or, report-only, preview).
    """

    report_only: bool
    cooldown: timedelta
    max_per_run: int


@dataclass(frozen=True, slots=True)
class BmcResetResult:
    """
    The result of one reset attempt.

    Attributes:
        outcome (ResetOutcome): What happened.
        bmc_host (str | None): The BMC address that was contacted.
        detail (str): A short reason, for the audit event and the log.
        active_jobs (tuple[str, ...]): `Id (JobState)` of each job that blocked the reset.
    """

    outcome: ResetOutcome
    bmc_host: str | None = None
    detail: str = ""
    active_jobs: tuple[str, ...] = ()


class BmcResetter(Protocol):
    """Restarts one server's BMC (not the host), unless the BMC is busy."""

    async def reset(self, server: Server, *, apply: bool) -> BmcResetResult:
        """
        Check the BMC's job queue and, when idle and `apply`, ask it to restart.

        Args:
            server (Server): The server whose BMC to restart.
            apply (bool): False reads the job queue and reports `WOULD_RESET`, sending nothing.

        Returns:
            BmcResetResult: The outcome; never raises for a per-host failure.
        """
        ...
