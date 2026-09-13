"""`ServerInventoryProvider` for standalone Redfish BMCs.

The first collector whose cost is per *server* rather than per manager:
one UCS Central run costs ~11 round trips for a whole fleet, while this
costs ~25 against each BMC, plus two more per installed GPU (its own
`ProcessorMetrics` and `EnvironmentMetrics`) — a real, GPU-count-scaled
cost on an 8-GPU DGX-class server, not a flat addition. Bounded
concurrency, a per-host wall-clock budget and a total-run budget are
therefore correctness requirements, not tuning knobs — see
docs/adr/0016-redfish-standalone-collector.md.

Failure is normal here rather than exceptional. A run where 40 of 400
hosts do not answer is a Tuesday, so per-host failures — a dead BMC and a
rejected login alike — are collected into `collection_errors` and the run
always continues to the next host. See ADR-0016's 2026-09-12 update for
why nothing skips ahead any more.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.infrastructure.providers.redfish.client import (
    RedfishAuthError,
    RedfishClient,
    RedfishError,
    RedfishForbiddenError,
    RedfishProtocolError,
    RedfishTlsError,
    RedfishUnreachableError,
    validate_odata_id,
)
from app.infrastructure.providers.redfish.mapping import (
    gpus_from_processors,
    has_only_gpu_processors,
    is_gpu_processor,
    psus_from_supplies,
    system_to_provider_server,
)
from app.infrastructure.providers.redfish.targets import RedfishTarget

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.REDFISH_STANDALONE.value

# `collection_errors` shapes, exported for `tools.run_collector` and `..openmanage`.
UNREACHABLE_MARKER = ": unreachable — "
AUTH_REJECTED_MARKER = ": login failed for credential "


class RedfishStandaloneProvider(ServerInventoryProvider):
    """
    Collects every BMC in the configured inventory.

    See docs/adr/0016-redfish-standalone-collector.md.
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        targets: list[RedfishTarget],
        connect_timeout: float,
        read_timeout: float,
        host_budget_seconds: float,
        run_budget_seconds: float,
        fleet_concurrency: int,
        tls_min_version: str = "TLSv1_2",
        debug_http: bool = False,
        client_factory: Callable[[RedfishTarget], Any] | None = None,
    ) -> None:
        """
        Build a collector for one inventory.

        Args:
            manager (Manager): The manager this run reports under.
            targets (list[RedfishTarget]): The validated fleet list.
            connect_timeout (float): Per-connection timeout.
            read_timeout (float): Per-response timeout.
            host_budget_seconds (float): Wall clock allowed per host.
            run_budget_seconds (float): Wall clock allowed for the run.
            fleet_concurrency (int): BMCs contacted at once.
            tls_min_version (str): Minimum TLS version.
            debug_http (bool): Emit one redacted line per request.
            client_factory (Callable[[RedfishTarget], Any] | None): Test
                seam returning a client for a target.
        """
        self._manager = manager
        self._targets = targets
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._host_budget = host_budget_seconds
        self._run_budget = run_budget_seconds
        self._concurrency = max(1, fleet_concurrency)
        self._tls_min_version = tls_min_version
        self._debug_http = debug_http
        super().__init__()
        self._auth_failures = 0
        self._client_factory: Callable[[RedfishTarget], Any] = client_factory or self._new_client

    def _new_client(self, target: RedfishTarget) -> RedfishClient:
        """
        Build a client for one target.

        Args:
            target (RedfishTarget): The BMC to reach.

        Returns:
            RedfishClient: An unauthenticated client; entering it logs in.
        """
        return RedfishClient(
            target=target,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            tls_min_version=self._tls_min_version,
            debug_http=self._debug_http,
        )

    async def health_check(self) -> None:
        """
        Verify the collector is configured well enough to run.

        Deliberately makes no network call (ADR-0016, 2026-09-13 update).

        Raises:
            ValueError: If the inventory is empty. `load_targets` already
                rejects that, so this is a belt-and-braces check for a
                provider constructed directly.
        """
        if not self._targets:
            raise ValueError("Redfish inventory is empty; there is nothing to collect.")

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Collect every host in the inventory.

        Each host's servers are yielded as it finishes, so a run killed at
        its deadline has already persisted what completed.

        Yields:
            ProviderServer: One per `ComputerSystem` found.
        """
        self._auth_failures = 0

        # Shuffled so a truncated sweep does not starve the same slow hosts.
        order = list(self._targets)
        random.shuffle(order)

        semaphore = asyncio.Semaphore(self._concurrency)
        tasks = [asyncio.create_task(self._collect_host(t, semaphore)) for t in order]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._run_budget
        collected = 0
        hosts_done = 0
        budget_expired = False
        try:
            # `as_completed(timeout=)` plus the deadline check, never
            # `asyncio.timeout()` around a generator — ADR-0016, 2026-09-13 update.
            for finished in asyncio.as_completed(tasks, timeout=self._run_budget):
                try:
                    batch = await finished
                except TimeoutError:
                    budget_expired = True
                    break
                hosts_done += 1
                for provider_server in batch:
                    if loop.time() >= deadline:
                        budget_expired = True
                        break
                    collected += 1
                    yield provider_server
                if budget_expired:
                    break
            if budget_expired:
                unfinished = len(tasks) - hosts_done
                logger.error(
                    "redfish.run_budget_exceeded",
                    budget_seconds=self._run_budget,
                    hosts_total=len(order),
                    hosts_unfinished=unfinished,
                    hint=(
                        "The run stopped itself before the CronJob's activeDeadlineSeconds could "
                        "kill it, so this summary exists. Raise "
                        "INVENTORY_REDFISH_RUN_BUDGET_SECONDS, lower the fleet size per CronJob, "
                        "or raise INVENTORY_REDFISH_FLEET_CONCURRENCY."
                    ),
                )
                self._record_error(
                    f"run budget of {self._run_budget:.0f}s expired with {unfinished} host(s) "
                    "not yet collected"
                )
        finally:
            for task in tasks:
                task.cancel()
            # Drained, so each cancelled host still runs its session teardown.
            await asyncio.gather(*tasks, return_exceptions=True)

        self._log_summary(total=len(order), collected=collected)

    def _log_summary(self, *, total: int, collected: int) -> None:
        """
        Emit one run summary, even when everything succeeded (ADR-0016, 2026-09-13 update).

        Args:
            total (int): Hosts attempted.
            collected (int): Servers yielded.
        """
        logger.info(
            "redfish.run_summary",
            hosts_total=total,
            servers_collected=collected,
            hosts_failed=len(self.collection_errors),
            auth_failures=self._auth_failures,
        )

    async def _collect_host(
        self, target: RedfishTarget, semaphore: asyncio.Semaphore
    ) -> list[ProviderServer]:
        """
        Collect one BMC, containing every failure to that host.

        Args:
            target (RedfishTarget): The BMC to collect.
            semaphore (asyncio.Semaphore): Limits concurrent hosts.

        Returns:
            list[ProviderServer]: Its servers, or an empty list on failure.
        """
        # Slot first, then the budget — queueing is not charged to the host.
        async with semaphore:
            if not target.verify_tls:
                logger.warning(
                    "redfish.tls_verification_disabled",
                    host=target.host,
                    reason=target.verify_tls_reason,
                    hint=(
                        "This BMC's password is sent to whatever answers at this address. "
                        "Import the issuing CA and remove the opt-out."
                    ),
                )

            try:
                async with asyncio.timeout(self._host_budget):
                    return await self._collect_systems(target)
            except TimeoutError:
                logger.warning(
                    "redfish.host_budget_exceeded",
                    host=target.host,
                    budget_seconds=self._host_budget,
                )
                self._record_error(f"{target.host}: exceeded its {self._host_budget:.0f}s budget")
            except RedfishAuthError as exc:
                self._record_auth_failure(target, exc)
            except RedfishTlsError as exc:
                logger.exception("redfish.tls_verify_failed", host=target.host, error=str(exc))
                self._record_error(f"{target.host}: TLS verification failed — {exc}")
            except RedfishUnreachableError as exc:
                # ERROR: the log may be the only signal left (UNREACHABLE_MARKER).
                logger.exception("redfish.host_unreachable", host=target.host, error=str(exc))
                self._record_error(f"{target.host}{UNREACHABLE_MARKER}{exc}")
            except (RedfishError, ValueError) as exc:
                logger.warning("redfish.host_failed", host=target.host, error=str(exc))
                self._record_error(f"{target.host}: {exc}")
            return []

    def _record_auth_failure(self, target: RedfishTarget, exc: RedfishAuthError) -> None:
        """
        Record a rejected login, loudly, and move on to the next host.

        Args:
            target (RedfishTarget): The host that rejected the credential.
            exc (RedfishAuthError): The rejection.
        """
        self._auth_failures += 1
        logger.error(
            "redfish.bmc_login_failed",
            host=target.host,
            credential=target.credential.name,
            error=str(exc),
            run_failures=self._auth_failures,
        )
        self._record_error(f"{target.host}{AUTH_REJECTED_MARKER}{target.credential.name!r}")

    async def _collect_systems(self, target: RedfishTarget) -> list[ProviderServer]:
        """
        Open a session and map every `ComputerSystem` the BMC exposes.

        A DGX/HGX GPU-baseboard tray is folded into its sibling host; an
        OpenBMC multi-host system is not (ADR-0016's DGX/HGX update).

        Args:
            target (RedfishTarget): The BMC to collect.

        Returns:
            list[ProviderServer]: One per physical machine — a
                GPU-baseboard tray's GPUs are folded into its sibling
                host rather than counted separately, whenever exactly
                one sibling host is present to fold them into.

        Raises:
            RedfishError: Propagated to `_collect_host`, which classifies
                it.
        """
        async with self._client_factory(target) as client:
            systems_link = client.service_root.get("Systems", {})
            systems_path = systems_link.get("@odata.id") if isinstance(systems_link, dict) else None
            if not systems_path:
                self._note_no_systems(target, reason="service root advertises no Systems")
                return []

            systems = await client.get_collection(validate_odata_id(systems_path))
            if not systems:
                self._note_no_systems(target, reason="Systems collection is empty")
                return []

            bmc_mac = await self._bmc_mac(client, systems[0])

            # Up front, so a tray is recognized before anything is mapped.
            fetched: list[
                tuple[dict[str, Any], list[dict[str, Any]] | None, dict[str, Any], dict[str, Any]]
            ] = []
            for system in systems:
                processors = await self._optional(client, system, "Processors")
                gpu_metrics, gpu_environment = await self._gpu_telemetry(client, processors)
                fetched.append((system, processors, gpu_metrics, gpu_environment))

            is_tray = [has_only_gpu_processors(f[1]) for f in fetched]
            trays = [f for f, tray in zip(fetched, is_tray, strict=True) if tray]
            hosts = [f for f, tray in zip(fetched, is_tray, strict=True) if not tray]

            extra_gpus: tuple[dict[str, object], ...] = ()
            emit = fetched
            if trays and len(hosts) == 1:
                merged: list[dict[str, object]] = []
                for tray_system, tray_processors, tray_metrics, tray_environment in trays:
                    tray_gpus = gpus_from_processors(
                        tray_processors,
                        metrics_by_processor=tray_metrics,
                        environment_by_processor=tray_environment,
                    )
                    merged.extend(tray_gpus or ())
                    logger.info(
                        "redfish.gpu_baseboard_merged",
                        host=target.host,
                        tray=str(tray_system.get("@odata.id") or tray_system.get("Id") or ""),
                        into=str(hosts[0][0].get("@odata.id") or hosts[0][0].get("Id") or ""),
                        gpus=len(tray_gpus or ()),
                    )
                extra_gpus = tuple(merged)
                emit = hosts  # the tray itself is not ingested as its own server
            elif trays:
                logger.warning(
                    "redfish.gpu_baseboard_ambiguous",
                    host=target.host,
                    trays=[str(t[0].get("@odata.id") or t[0].get("Id") or "") for t in trays],
                    hosts=len(hosts),
                    hint=(
                        "Found a GPU-only ComputerSystem (all-GPU Processors, no CPU) but "
                        "could not identify exactly one sibling host to merge it into, so "
                        "every system is being ingested separately instead."
                    ),
                )

            collected: list[ProviderServer] = []
            for system, processors, gpu_metrics, gpu_environment in emit:
                collected.append(
                    system_to_provider_server(
                        system,
                        host=target.host,
                        base_url=target.base_url,
                        manager_id=self._manager.id,
                        override_name=target.name,
                        processors=processors,
                        drives=await self._drives(client, system),
                        dimms=await self._optional(client, system, "Memory"),
                        interfaces=await self._optional(client, system, "EthernetInterfaces"),
                        bmc_mac=bmc_mac,
                        psus=psus_from_supplies(await self._psus(client, system)),
                        gpu_metrics_by_processor=gpu_metrics,
                        gpu_environment_by_processor=gpu_environment,
                        extra_gpus=extra_gpus,
                    )
                )
        return collected

    def _note_no_systems(self, target: RedfishTarget, *, reason: str) -> None:
        """
        Record a BMC that authenticated but exposes no server (ADR-0016, 2026-09-13 update).

        Args:
            target (RedfishTarget): The BMC.
            reason (str): What was observed.
        """
        logger.warning(
            "redfish.no_systems",
            host=target.host,
            reason=reason,
            hint=(
                "The BMC authenticated but exposes no ComputerSystem. Check the address is a "
                "server's BMC rather than a chassis or enclosure manager, and that Redfish is "
                "licensed on this hardware."
            ),
        )
        self._record_error(f"{target.host}: authenticated but exposes no system")

    async def _optional(
        self, client: Any, system: dict[str, Any], key: str
    ) -> list[dict[str, Any]] | None:
        """
        Read a sub-collection, tolerating a BMC that cannot serve it.

        `None` (never `[]`) on failure, which ingest carries forward.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The owning `ComputerSystem`.
            key (str): Link property to follow.

        Returns:
            list[dict[str, Any]] | None: The members, or None if the
                collection is absent or could not be read.
        """
        link = system.get(key)
        path = link.get("@odata.id") if isinstance(link, dict) else None
        if not path:
            return None
        try:
            members: list[dict[str, Any]] = await client.get_collection(validate_odata_id(path))
        except (RedfishForbiddenError, RedfishProtocolError, RedfishUnreachableError) as exc:
            logger.warning(
                "redfish.resource_skipped", host=system.get("Id"), resource=key, error=str(exc)
            )
            return None
        else:
            return members

    async def _drives(self, client: Any, system: dict[str, Any]) -> list[dict[str, Any]] | None:
        """
        Read every drive behind a system's `Storage` controllers.

        Each `Drives[].@odata.id` is followed (ADR-0016, 2026-09-13 update).

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The owning `ComputerSystem`.

        Returns:
            list[dict[str, Any]] | None: Every drive, deduplicated by
                `@odata.id`, or None if storage could not be read at all.
        """
        controllers = await self._optional(client, system, "Storage")
        if controllers is None:
            return None
        drives: dict[str, dict[str, Any]] = {}
        for controller in controllers:
            for link in controller.get("Drives", []) or []:
                if not isinstance(link, dict):
                    continue
                try:
                    path = validate_odata_id(link.get("@odata.id"))
                    if path not in drives:
                        drives[path] = await client.get(path)
                except (RedfishForbiddenError, RedfishProtocolError) as exc:
                    logger.warning("redfish.drive_skipped", error=str(exc))
        return list(drives.values())

    async def _gpu_telemetry(
        self, client: Any, processors: list[dict[str, Any]] | None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """
        Read each GPU's own `ProcessorMetrics` and `EnvironmentMetrics`.

        Two requests per GPU, each failing to `None` rather than failing the
        host (ADR-0016's GPU telemetry update).

        Args:
            client (Any): The authenticated client.
            processors (list[dict[str, Any]] | None): The system's
                `Processors` members, or None when unread — in which
                case there is nothing to look up telemetry for.

        Returns:
            tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
                `(metrics_by_processor, environment_by_processor)`, each
                keyed by the owning processor's `@odata.id`.
        """
        metrics: dict[str, dict[str, Any]] = {}
        environment: dict[str, dict[str, Any]] = {}
        for processor in processors or []:
            if not is_gpu_processor(processor):
                continue
            processor_id = str(processor.get("@odata.id") or "")
            if not processor_id:
                continue
            fetched_metrics = await self._optional_link(client, processor, "Metrics")
            if fetched_metrics is not None:
                metrics[processor_id] = fetched_metrics
            fetched_environment = await self._optional_link(client, processor, "EnvironmentMetrics")
            if fetched_environment is not None:
                environment[processor_id] = fetched_environment
        return metrics, environment

    async def _optional_link(
        self, client: Any, resource: dict[str, Any], key: str
    ) -> dict[str, Any] | None:
        """
        Fetch a single linked sub-resource, tolerating a BMC that cannot serve it.

        The single-resource analogue of `_optional`, which follows a link to
        a *collection* (`Processor.Metrics`, `.EnvironmentMetrics` here).

        Args:
            client (Any): The authenticated client.
            resource (dict[str, Any]): The resource carrying the link.
            key (str): Link property to follow.

        Returns:
            dict[str, Any] | None: The resource, or None if the link is
                absent or could not be read.
        """
        link = resource.get(key)
        path = link.get("@odata.id") if isinstance(link, dict) else None
        if not path:
            return None
        try:
            resolved: dict[str, Any] = await client.get(validate_odata_id(path))
        except (RedfishForbiddenError, RedfishProtocolError, RedfishUnreachableError) as exc:
            logger.warning("redfish.resource_skipped", resource=key, error=str(exc))
            return None
        else:
            return resolved

    async def _psus(self, client: Any, system: dict[str, Any]) -> list[dict[str, Any]] | None:
        """
        Read a system's power supplies through `Links.Chassis`.

        `PowerSubsystem/PowerSupplies` first, then the deprecated `Power`
        resource (ADR-0016, 2026-09-13 update).

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The `ComputerSystem` to start from.

        Returns:
            list[dict[str, Any]] | None: `PowerSupply` resources, or None
                when there is no chassis link or neither path could be
                read. None means unread, which `_carry_forward` keeps
                stored PSUs across — an empty list would clear them.
        """
        links = system.get("Links", {})
        chassis_refs = links.get("Chassis", []) if isinstance(links, dict) else []
        if not chassis_refs or not isinstance(chassis_refs[0], dict):
            return None
        try:
            chassis = await client.get(validate_odata_id(chassis_refs[0].get("@odata.id")))
        except (RedfishForbiddenError, RedfishProtocolError, RedfishUnreachableError) as exc:
            logger.warning("redfish.resource_skipped", resource="Chassis", error=str(exc))
            return None

        subsystem = await self._optional_link(client, chassis, "PowerSubsystem")
        if subsystem is not None:
            supplies = await self._optional(client, subsystem, "PowerSupplies")
            if supplies is not None:
                return supplies

        power = await self._optional_link(client, chassis, "Power")
        if power is None:
            return None
        inline = power.get("PowerSupplies")
        return inline if isinstance(inline, list) else None

    async def _bmc_mac(self, client: Any, system: dict[str, Any]) -> str | None:
        """
        Read the BMC's own MAC, once per host.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): Any system on this BMC, used to reach
                `Links.ManagedBy`.

        Returns:
            str | None: The manager's MAC, or None if unreachable.
        """
        links = system.get("Links", {})
        managed_by = links.get("ManagedBy", []) if isinstance(links, dict) else []
        if not managed_by or not isinstance(managed_by[0], dict):
            return None
        with contextlib.suppress(RedfishError):
            manager = await client.get(validate_odata_id(managed_by[0].get("@odata.id")))
            interfaces = await self._optional(client, manager, "EthernetInterfaces")
            for interface in interfaces or []:
                mac = interface.get("PermanentMACAddress") or interface.get("MACAddress")
                if isinstance(mac, str) and mac.strip():
                    return mac.strip()
        return None
