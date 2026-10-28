"""Restart one iDRAC over Redfish, only when its job queue is idle (ADR-0050).

`Manager.Reset` with `GracefulRestart` restarts the iDRAC, never the host. The
job states and paths below follow Dell's Redfish documentation and are not yet
verified live: a report-only run reads the queue and logs what it would do.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import structlog

from app.domain.models.server import Server
from app.domain.ports.bmc_reset import BmcResetResult, ResetOutcome
from app.infrastructure.providers.redfish.client import (
    RedfishClient,
    RedfishError,
    RedfishForbiddenError,
    RedfishUnreachableError,
)
from app.infrastructure.providers.redfish.targets import RedfishCredential, RedfishTarget

logger = structlog.get_logger(__name__)

_MANAGERS = "/redfish/v1/Managers"
_SYSTEMS = "/redfish/v1/Systems"
_JOBS = "/redfish/v1/JobService/Jobs?$expand=*($levels=1)"
# Anything else, including a missing state, blocks: an unknown job is treated as running.
# `Scheduled` waits for the next host reboot and can sit for weeks, so it must not block.
_IDLE_JOB_STATES = frozenset(
    {"Completed", "CompletedWithErrors", "Failed", "RebootFailed", "Scheduled"}
)
_MAX_REPORTED_JOBS = 5


class IdracResetter:
    """Resets the iDRAC of a Dell server with the one shared BMC login."""

    def __init__(
        self,
        *,
        credential: RedfishCredential,
        port: int,
        verify_tls: bool,
        ca_bundle: str | None,
        connect_timeout: float,
        read_timeout: float,
        tls_min_version: str = "TLSv1_2",
        client_factory: Callable[[RedfishTarget], RedfishClient] | None = None,
        power_on_wait_seconds: float = 120.0,
        poll_seconds: float = 5.0,
    ) -> None:
        """
        Bind the resetter to the iDRAC login and TLS settings. No I/O happens here.

        Args:
            credential (RedfishCredential): The iDRAC account, with the Configure Manager privilege.
            port (int): HTTPS port every iDRAC answers on.
            verify_tls (bool): Verify the iDRAC certificate.
            ca_bundle (str | None): PEM bundle trusted in addition to the system store.
            connect_timeout (float): Seconds to establish a connection.
            read_timeout (float): Seconds to wait for a response body.
            tls_min_version (str): Minimum TLS version name.
            client_factory (Callable[[RedfishTarget], RedfishClient] | None): A test seam.
            power_on_wait_seconds (float): How long to wait for a powered-on host to report `On`.
            poll_seconds (float): Seconds between `PowerState` reads while waiting.
        """
        self._power_on_wait = power_on_wait_seconds
        self._poll_seconds = poll_seconds
        self._credential = credential
        self._port = port
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._tls_min_version = tls_min_version
        self._client_factory = client_factory

    async def reset(self, server: Server, *, apply: bool) -> BmcResetResult:
        """
        Check the job queue and, when idle and `apply`, restart the server's iDRAC.

        Args:
            server (Server): The server whose iDRAC to restart.
            apply (bool): False reads the queue and reports `WOULD_RESET`, sending nothing.

        Returns:
            BmcResetResult: The outcome; a per-host failure is returned, never raised.
        """
        host = server.network.bmc.host
        if not host:
            return BmcResetResult(ResetOutcome.FAILED, detail="the server has no BMC host")
        target = RedfishTarget(
            host=host,
            port=self._port,
            credential=self._credential,
            verify_tls=self._verify_tls,
            verify_tls_reason=None if self._verify_tls else "INVENTORY_OME_BMC_VERIFY_TLS is off",
            ca_bundle=self._ca_bundle,
            name=None,
        )
        client = (
            self._client_factory(target)
            if self._client_factory
            else RedfishClient(
                target=target,
                connect_timeout=self._connect_timeout,
                read_timeout=self._read_timeout,
                tls_min_version=self._tls_min_version,
            )
        )
        try:
            async with client:
                manager = await self._manager_path(client)
                blocking = await self._blocking_jobs(client)
                if blocking:
                    return BmcResetResult(
                        ResetOutcome.SKIPPED_ACTIVE_JOBS,
                        host,
                        "the iDRAC job queue is busy",
                        blocking,
                    )
                system = await self._system_path(client)
                state = await self._power_state(client, system)
                off = state == "Off"
                if not apply:
                    return BmcResetResult(
                        ResetOutcome.WOULD_RESET,
                        host,
                        "report-only" + ("; would power the host on first" if off else ""),
                        power_state=state,
                    )
                powered_on = False
                if off:
                    if server.maintenance.enabled:
                        return BmcResetResult(
                            ResetOutcome.FAILED,
                            host,
                            "the host is powered off and in maintenance; not powering it on",
                            power_state=state,
                        )
                    failure = await self._power_on(client, system, host)
                    if failure is not None:
                        return replace(failure, power_state=state)
                    powered_on = True
                result = await self._post_reset(client, manager, host)
                return BmcResetResult(
                    result.outcome,
                    result.bmc_host,
                    result.detail,
                    powered_on=powered_on,
                    power_state=state,
                )
        except RedfishForbiddenError as exc:
            return BmcResetResult(
                ResetOutcome.FAILED, host, f"the iDRAC account is forbidden: {exc}"
            )
        except RedfishError as exc:
            return BmcResetResult(ResetOutcome.FAILED, host, str(exc))

    async def _manager_path(self, client: RedfishClient) -> str:
        """
        Find the iDRAC's own Manager resource.

        Args:
            client (RedfishClient): An open session.

        Returns:
            str: The Manager's `@odata.id`, the `iDRAC` member or else the first.

        Raises:
            RedfishError: If the BMC lists no Manager.
        """
        members = [
            m["@odata.id"]
            for m in (await client.get(_MANAGERS)).get("Members", [])
            if isinstance(m, dict) and isinstance(m.get("@odata.id"), str)
        ]
        if not members:
            raise RedfishError(f"{_MANAGERS} lists no Manager")
        return next((m for m in members if "idrac" in m.lower()), members[0])

    async def _system_path(self, client: RedfishClient) -> str:
        """
        Find the host's ComputerSystem resource.

        Args:
            client (RedfishClient): An open session.

        Returns:
            str: The first System's `@odata.id`.

        Raises:
            RedfishError: If the BMC lists no System.
        """
        for member in (await client.get(_SYSTEMS)).get("Members", []):
            if isinstance(member, dict) and isinstance(member.get("@odata.id"), str):
                return member["@odata.id"]
        raise RedfishError(f"{_SYSTEMS} lists no System")

    async def _power_state(self, client: RedfishClient, system: str) -> str | None:
        """
        Read the host's `PowerState`.

        Args:
            client (RedfishClient): An open session.
            system (str): The System's `@odata.id`.

        Returns:
            str | None: `On`, `Off`, ... or `None` when the BMC does not report it.
        """
        state = (await client.get(system)).get("PowerState")
        return state if isinstance(state, str) else None

    async def _power_on(
        self, client: RedfishClient, system: str, host: str
    ) -> BmcResetResult | None:
        """
        Power the host on and wait until it reports `On`.

        Args:
            client (RedfishClient): An open session.
            system (str): The System's `@odata.id`.
            host (str): The BMC address, for the result.

        Returns:
            BmcResetResult | None: A `FAILED` result when the host did not come on in time
                (the iDRAC is then left alone), `None` once it is on.
        """
        await client.post(f"{system}/Actions/ComputerSystem.Reset", {"ResetType": "On"})
        deadline = time.monotonic() + self._power_on_wait
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_seconds)
            if await self._power_state(client, system) == "On":
                logger.warning("idrac_reset.powered_on", host=host)
                return None
        return BmcResetResult(
            ResetOutcome.FAILED,
            host,
            f"the host did not report On within {self._power_on_wait:.0f}s of the power-on",
            powered_on=True,
        )

    async def _blocking_jobs(self, client: RedfishClient) -> tuple[str, ...]:
        """
        List the jobs that are not finished, newest-agnostic and capped for the audit event.

        Args:
            client (RedfishClient): An open session.

        Returns:
            tuple[str, ...]: `Id (JobState)` for each blocking job, at most five.
        """
        blocking: list[str] = []
        job: dict[str, Any]
        for job in await client.get_collection(_JOBS):
            state = job.get("JobState")
            if state not in _IDLE_JOB_STATES:
                blocking.append(f"{job.get('Id', '?')} ({state})")
        return tuple(blocking[:_MAX_REPORTED_JOBS])

    async def _post_reset(self, client: RedfishClient, manager: str, host: str) -> BmcResetResult:
        """
        POST the restart once.

        Args:
            client (RedfishClient): An open session.
            manager (str): The Manager's `@odata.id`.
            host (str): The BMC address, for the result.

        Returns:
            BmcResetResult: `REQUESTED`, or `UNCONFIRMED` when the iDRAC dropped the connection.
        """
        try:
            await client.post(f"{manager}/Actions/Manager.Reset", {"ResetType": "GracefulRestart"})
        except RedfishUnreachableError as exc:
            logger.warning("idrac_reset.unconfirmed", host=host, error=str(exc))
            return BmcResetResult(
                ResetOutcome.UNCONFIRMED, host, "the iDRAC dropped the connection"
            )
        return BmcResetResult(ResetOutcome.REQUESTED, host)
