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
import dataclasses
import random
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar
from typing import Any

import structlog

from app.domain.enums import ManagerType, UnreachableReason, Vendor
from app.domain.models.manager import Manager
from app.domain.ports.provider import (
    SERVER_FAILED_MARKER,
    ProviderServer,
    ServerIdentity,
    ServerInventoryProvider,
)
from app.infrastructure.providers.redfish import raid
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
    _BUILTIN_PCI_DEVICE_MODELS,
    _clean_serial,
    _dell_serial,
    _server_name,
    controllers_from_storage,
    gpus_from_pcie_devices,
    gpus_from_processors,
    has_only_gpu_processors,
    is_gpu_processor,
    pcie_device_refs,
    psus_from_supplies,
    system_to_provider_server,
    vendor_from_manufacturer,
)
from app.infrastructure.providers.redfish.targets import RedfishTarget

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.REDFISH_STANDALONE.value

# `collection_errors` shapes, exported for `tools.run_collector` and `..openmanage`.
UNREACHABLE_MARKER = ": unreachable — "
AUTH_REJECTED_MARKER = ": login failed for credential "
TLS_FAILED_MARKER = ": TLS verification failed — "
BUDGET_MARKER = ": exceeded its "
UNEXPECTED_MARKER = SERVER_FAILED_MARKER

# A tree the BMC does not implement, as opposed to one it failed to serve this time.
_TREE_ABSENT_STATUSES = frozenset({404, 501, 503})

_PCIE_SELECT = "$select=Manufacturer,Description,Status"

# Failures `_collect_host` classifies; a per-system handler must let them through.
_HOST_LEVEL_ERRORS = (RedfishAuthError, RedfishTlsError, RedfishUnreachableError, TimeoutError)

# The BMC host and System being read, so every `redfish.*` warning can name them.
_SCOPE: ContextVar[dict[str, str] | None] = ContextVar("redfish_scope", default=None)
_UNFINISHED_LISTED = 10


def _system_path(system: object) -> str:
    """
    Name a `ComputerSystem` for a log line.

    Args:
        system (object): The raw System body.

    Returns:
        str: Its `@odata.id`, else its `Id`, else an empty string.
    """
    if not isinstance(system, dict):
        return ""
    return str(system.get("@odata.id") or system.get("Id") or "")


def _enter_system(system: object) -> None:
    """
    Record which System the current task is reading.

    Args:
        system (object): The raw System body.
    """
    _SCOPE.set({**(_SCOPE.get() or {}), "system": _system_path(system)})


def _warn(event: str, **fields: object) -> None:
    """
    Log a warning carrying the current BMC host and System.

    Args:
        event (str): The log event.
        **fields (object): Extra fields, which win over the scope's.
    """
    logger.warning(event, **{**(_SCOPE.get() or {}), **fields})


def _hosts_summary(hosts: list[str]) -> str:
    """
    Join host names, capped so a dead fleet does not flood one line.

    Args:
        hosts (list[str]): Host names.

    Returns:
        str: At most ten names, then "and N more".
    """
    shown = ", ".join(hosts[:_UNFINISHED_LISTED])
    extra = len(hosts) - _UNFINISHED_LISTED
    return f"{shown} and {extra} more" if extra > 0 else shown


def reason_from_error(message: str) -> UnreachableReason:
    """
    Classify one `collection_errors` entry into an `UnreachableReason`.

    Args:
        message (str): An entry recorded by `_collect_host`.

    Returns:
        UnreachableReason: The reason its marker names, else `PROTOCOL_ERROR`.
    """
    for marker, reason in (
        (UNEXPECTED_MARKER, UnreachableReason.COLLECTION_ERROR),
        (UNREACHABLE_MARKER, UnreachableReason.NETWORK_UNREACHABLE),
        (AUTH_REJECTED_MARKER, UnreachableReason.AUTH_REJECTED),
        (TLS_FAILED_MARKER, UnreachableReason.TLS_ERROR),
        (BUDGET_MARKER, UnreachableReason.TIMEOUT),
    ):
        if marker in message:
            return reason
    return UnreachableReason.PROTOCOL_ERROR


def _pcie_scan_query(service_root: dict[str, Any]) -> str:
    """
    Build the query string a `PCIeDevices` scan uses, narrowed to what the BMC advertises.

    See ADR-0016's 2026-09-15 PCIeDevice update for the DSP0266 query
    parameters this checks and why `.` (never `~`/`*`) is requested.

    Args:
        service_root (dict[str, Any]): The client's own `service_root`,
            already fetched at login.

    Returns:
        str: `"?$expand=.($levels=1)&$select=..."` when the BMC
            advertises `ExpandQuery.NoLinks`, else `"?$select=..."`.
    """
    protocol = service_root.get("ProtocolFeaturesSupported")
    protocol = protocol if isinstance(protocol, dict) else {}
    expand = protocol.get("ExpandQuery")
    expand = expand if isinstance(expand, dict) else {}
    if expand.get("NoLinks"):
        return f"?$expand=.($levels=1)&{_PCIE_SELECT}"
    return f"?{_PCIE_SELECT}"


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
        pcie_gpu_detection: bool = False,
        pcie_gpu_max_devices: int = 50,
        pcie_gpu_max_gpus: int = 16,
        pcie_gpu_models: dict[tuple[str, str], str] | None = None,
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
            pcie_gpu_detection (bool): Screen `PCIeDevices` for GPUs when
                a system's `Processors` reports none — see ADR-0016's
                2026-09-15 PCIeDevice update.
            pcie_gpu_max_devices (int): Stop paging a system's
                `PCIeDevices` collection once this many entries have
                been seen.
            pcie_gpu_max_gpus (int): Stop once this many GPUs are found
                among the scanned devices.
            pcie_gpu_models (dict[tuple[str, str], str] | None): PCI ID
                -> model overrides, already merged over the built-in
                table by the factory — see ADR-0016's 2026-09-16 update.
                None uses the built-in table alone.
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
        self._pcie_gpu_detection = pcie_gpu_detection
        self._pcie_gpu_max_devices = pcie_gpu_max_devices
        self._pcie_gpu_max_gpus = pcie_gpu_max_gpus
        self._pcie_gpu_models = (
            _BUILTIN_PCI_DEVICE_MODELS if pcie_gpu_models is None else pcie_gpu_models
        )
        super().__init__()
        self._auth_failures = 0
        self._finished_hosts: set[str] = set()
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

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Fetch one server's current state from its own BMC (ADR-0032).

        Already single-host in shape, so this reuses `_collect_systems`
        for just the one matching target.

        Args:
            identity (ServerIdentity): `host` selects the target;
                `serial` disambiguates when one BMC reports more than one
                `ComputerSystem`.

        Returns:
            ProviderServer | None: The current state, or `None` if
                `host` isn't in this run's configured targets, or the BMC
                reports no matching system.
        """
        target = next((t for t in self._targets if t.host == identity.host), None)
        if target is None:
            logger.warning(
                "redfish.get_one_failed",
                host=identity.host,
                error="host is not in this run's configured targets",
            )
            return None
        try:
            async with asyncio.timeout(self._host_budget):
                servers = await self._collect_systems(target)
        except (RedfishError, ValueError, TimeoutError) as exc:
            logger.warning(
                "redfish.get_one_failed",
                host=target.host,
                error_type=type(exc).__name__,
                error=str(exc) or "timed out",
            )
            return None
        if not servers:
            logger.warning(
                "redfish.get_one_failed", host=target.host, error="the BMC reported no system"
            )
            return None
        if identity.serial is not None:
            return next((s for s in servers if s.serial == identity.serial), servers[0])
        return servers[0]

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Collect every host in the inventory.

        Each host's servers are yielded as it finishes, so a run killed at
        its deadline has already persisted what completed.

        Yields:
            ProviderServer: One per `ComputerSystem` found.
        """
        self._auth_failures = 0
        self._finished_hosts: set[str] = set()

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
                unfinished_hosts = [t.host for t in order if t.host not in self._finished_hosts]
                unfinished = len(unfinished_hosts)
                logger.error(
                    "redfish.run_budget_exceeded",
                    budget_seconds=self._run_budget,
                    hosts_total=len(order),
                    hosts_unfinished=unfinished,
                    unfinished_hosts=_hosts_summary(unfinished_hosts),
                    hint=(
                        "The run stopped itself before the CronJob's activeDeadlineSeconds could "
                        "kill it, so this summary exists. Raise "
                        "INVENTORY_REDFISH_RUN_BUDGET_SECONDS, lower the fleet size per CronJob, "
                        "or raise INVENTORY_REDFISH_FLEET_CONCURRENCY."
                    ),
                )
                self._record_error(
                    f"run budget of {self._run_budget:.0f}s expired with {unfinished} host(s) "
                    f"not yet collected: {_hosts_summary(unfinished_hosts)}"
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
            list[ProviderServer]: Its servers, or one `reachable=False` stub
                (ADR-0037) on failure.
        """
        reason = UnreachableReason.PROTOCOL_ERROR
        detail = "collection failed"
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
                self._record_error(f"{target.host}{BUDGET_MARKER}{self._host_budget:.0f}s budget")
                reason = UnreachableReason.TIMEOUT
                detail = f"timed out after {self._host_budget:.0f}s budget"
            except RedfishAuthError as exc:
                self._record_auth_failure(target, exc)
                reason = UnreachableReason.AUTH_REJECTED
                detail = f"login rejected for credential {target.credential.name!r}"
            except RedfishTlsError as exc:
                logger.exception("redfish.tls_verify_failed", host=target.host, error=str(exc))
                self._record_error(f"{target.host}{TLS_FAILED_MARKER}{exc}")
                reason = UnreachableReason.TLS_ERROR
                detail = f"TLS verification failed: {exc}"
            except RedfishUnreachableError as exc:
                # ERROR: the log may be the only signal left (UNREACHABLE_MARKER).
                logger.exception("redfish.host_unreachable", host=target.host, error=str(exc))
                self._record_error(f"{target.host}{UNREACHABLE_MARKER}{exc}")
                reason = UnreachableReason.NETWORK_UNREACHABLE
                detail = f"unreachable: {exc}"
            except (RedfishError, ValueError) as exc:
                logger.warning("redfish.host_failed", host=target.host, error=str(exc))
                self._record_error(f"{target.host}: {exc}")
                detail = f"protocol error: {exc}"
            except Exception as exc:
                logger.exception(
                    "redfish.host_failed_unexpected",
                    host=target.host,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                self._record_error(
                    f"{target.host}{UNEXPECTED_MARKER}collecting its systems: "
                    f"unexpected {type(exc).__name__}: {exc}"
                )
                self._server_failures += 1
                reason = UnreachableReason.COLLECTION_ERROR
                detail = f"unexpected {type(exc).__name__}: {exc}"
            finally:
                self._finished_hosts.add(target.host)
            return [self._unreachable_stub(target, reason, detail)]

    def _unreachable_stub(
        self, target: RedfishTarget, reason: UnreachableReason, detail: str
    ) -> ProviderServer:
        """
        Build the serial-less placeholder for a host that gave nothing (ADR-0037).

        Args:
            target (RedfishTarget): The failed BMC.
            reason (UnreachableReason): Why it failed.
            detail (str): The cause in words, shown to the operator.

        Returns:
            ProviderServer: `reachable=False`, every hardware field `None`.
        """
        return ProviderServer(
            external_id=f"redfish-unreachable:{target.host}",
            vendor=Vendor.STANDALONE.value,
            name=target.name or target.host,
            serial=None,
            bmc_address_raw=target.base_url.replace("https://", "redfish://"),
            manager_id=self._manager.id,
            reachable=False,
            unreachable_reason=reason,
            unreachable_detail=detail,
        )

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
        token = _SCOPE.set({"host": target.host})
        try:
            return await self._collect_systems_scoped(target)
        finally:
            _SCOPE.reset(token)

    async def _collect_systems_scoped(self, target: RedfishTarget) -> list[ProviderServer]:
        """
        Do `_collect_systems`' work inside its logging scope.

        Args:
            target (RedfishTarget): The BMC to collect.

        Returns:
            list[ProviderServer]: As `_collect_systems`.
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

            _enter_system(systems[0])
            bmc_mac = await self._bmc_mac(client, systems[0])
            collected: list[ProviderServer] = []

            # Up front, so a tray is recognized before anything is mapped.
            fetched: list[
                tuple[dict[str, Any], list[dict[str, Any]] | None, dict[str, Any], dict[str, Any]]
            ] = []
            is_tray: list[bool] = []
            early_failed = False
            for system in systems:
                _enter_system(system)
                try:
                    processors = await self._optional(client, system, "Processors")
                    gpu_metrics, gpu_environment = await self._gpu_telemetry(client, processors)
                    tray = has_only_gpu_processors(processors)
                except Exception as exc:
                    if isinstance(exc, _HOST_LEVEL_ERRORS):
                        raise
                    stub = self._system_failure(
                        target, system, exc, "reading its sub-resources", multi=len(systems) > 1
                    )
                    early_failed = True
                    # Tray or host is unknown here: on a multi-system BMC the stub could be a
                    # ghost document for a tray, so only a lone System gets one.
                    if stub is not None and len(systems) == 1:
                        collected.append(stub)
                    continue
                fetched.append((system, processors, gpu_metrics, gpu_environment))
                is_tray.append(tray)

            trays = [f for f, tray in zip(fetched, is_tray, strict=True) if tray]
            hosts = [f for f, tray in zip(fetched, is_tray, strict=True) if not tray]

            extra_gpus: tuple[dict[str, object], ...] = ()
            merge_error: Exception | None = None
            emit = fetched
            if trays and len(hosts) == 1:
                merged: list[dict[str, object]] = []
                try:
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
                            tray=_system_path(tray_system),
                            into=_system_path(hosts[0][0]),
                            gpus=len(tray_gpus or ()),
                        )
                except Exception as exc:
                    merge_error = exc
                extra_gpus = tuple(merged)
                emit = hosts  # the tray itself is not ingested as its own server
            elif trays:
                logger.warning(
                    "redfish.gpu_baseboard_ambiguous",
                    host=target.host,
                    trays=[_system_path(t[0]) for t in trays],
                    hosts=len(hosts),
                    hint=(
                        "Found a GPU-baseboard ComputerSystem (has a GPU, no CPU) but could "
                        "not identify exactly one sibling host to merge it into, so every "
                        "system is being ingested separately instead."
                    ),
                )

            for system, processors, gpu_metrics, gpu_environment in emit:
                _enter_system(system)
                try:
                    if merge_error is not None:
                        raise merge_error
                    mapped = await self._map_system(
                        client,
                        target,
                        system,
                        processors,
                        gpu_metrics,
                        gpu_environment,
                        bmc_mac,
                        extra_gpus,
                    )
                    if early_failed:  # the failed sibling may be its GPU tray: GPUs are unread
                        mapped = dataclasses.replace(mapped, gpus=None)
                    collected.append(mapped)
                except Exception as exc:
                    if isinstance(exc, _HOST_LEVEL_ERRORS):
                        raise
                    stage = "merging its GPU baseboard" if exc is merge_error else "mapping"
                    stub = self._system_failure(target, system, exc, stage, multi=len(systems) > 1)
                    if stub is not None:
                        collected.append(stub)
        return collected

    async def _map_system(
        self,
        client: Any,
        target: RedfishTarget,
        system: dict[str, Any],
        processors: list[dict[str, Any]] | None,
        gpu_metrics: dict[str, Any],
        gpu_environment: dict[str, Any],
        bmc_mac: str | None,
        extra_gpus: tuple[dict[str, object], ...],
    ) -> ProviderServer:
        """
        Read one `ComputerSystem`'s remaining sub-resources and map it.

        Args:
            client (Any): The authenticated client.
            target (RedfishTarget): The BMC.
            system (dict[str, Any]): The raw System.
            processors (list[dict[str, Any]] | None): Its `Processors`, when read.
            gpu_metrics (dict[str, Any]): Per-GPU `ProcessorMetrics`.
            gpu_environment (dict[str, Any]): Per-GPU `EnvironmentMetrics`.
            bmc_mac (str | None): The BMC's own MAC.
            extra_gpus (tuple[dict[str, object], ...]): GPUs merged from a baseboard tray.

        Returns:
            ProviderServer: The mapped server.
        """
        system_extra_gpus = extra_gpus
        if self._pcie_gpu_detection and not gpus_from_processors(
            processors,
            metrics_by_processor=gpu_metrics,
            environment_by_processor=gpu_environment,
        ):
            pcie_gpus = await self._pcie_gpus(client, system)
            if pcie_gpus:
                system_extra_gpus = system_extra_gpus + pcie_gpus
                logger.info(
                    "redfish.pcie_gpu_fallback_used",
                    host=target.host,
                    system=_system_path(system),
                    gpus=len(pcie_gpus),
                )
        supplies = await self._psus(client, system)
        psu_metrics = await self._psu_telemetry(client, supplies)
        storage, storage_status = await self._storage_collection(client, system)
        storage_controllers, raid_check = await self._raid(client, system, storage, storage_status)
        mapped = system_to_provider_server(
            system,
            host=target.host,
            base_url=target.base_url,
            manager_id=self._manager.id,
            override_name=target.name,
            processors=processors,
            drives=await self._drives(client, storage),
            dimms=await self._optional(client, system, "Memory"),
            interfaces=await self._optional(client, system, "EthernetInterfaces"),
            bmc_mac=bmc_mac,
            storage_controllers=storage_controllers,
            raid_check=raid_check,
            psus=psus_from_supplies(supplies, metrics_by_supply=psu_metrics),
            gpu_metrics_by_processor=gpu_metrics,
            gpu_environment_by_processor=gpu_environment,
            extra_gpus=system_extra_gpus,
            chassis=await self._chassis_fallback(client, system),
        )
        if mapped.serial is None:
            logger.warning(
                "redfish.no_serial",
                host=target.host,
                system=_system_path(system),
                hint=(
                    "Neither ComputerSystem.SerialNumber nor Chassis.SerialNumber "
                    "resolved; correlation cannot recognize this machine again and a "
                    "new document will be minted on the next run."
                ),
            )
        return mapped

    def _system_failure(
        self, target: RedfishTarget, system: object, exc: Exception, stage: str, *, multi: bool
    ) -> ProviderServer | None:
        """
        Contain one System's failure so its siblings on the same BMC survive.

        Args:
            target (RedfishTarget): The BMC.
            system (object): The raw System body, possibly malformed.
            exc (Exception): What went wrong.
            stage (str): What was being done.
            multi (bool): The BMC exposes several Systems, so a serial-less stub would be
                matched by BMC host to the wrong one.

        Returns:
            ProviderServer | None: A `COLLECTION_ERROR` stub, `None` without a safe identity.
        """
        body = system if isinstance(system, dict) else {}
        odata = body.get("@odata.id")
        manufacturer = body.get("Manufacturer")
        try:
            vendor = vendor_from_manufacturer(manufacturer).value
            serial = _dell_serial(body) or _clean_serial(body.get("SerialNumber"))
        except Exception:
            vendor, serial = Vendor.STANDALONE.value, None
        try:
            mapped_name = _server_name(body)
        except Exception:
            mapped_name = target.host
        stub = self._contain_server_failure(
            exc,
            event="redfish.system_failed",
            stage=stage,
            vendor=vendor,
            external_id=f"redfish://{target.host}{odata if isinstance(odata, str) else ''}",
            name=target.name or mapped_name,
            serial=serial,
            manager_id=self._manager.id,
            bmc_address_raw=target.base_url.replace("https://", "redfish://"),
            host=target.host,
            system=_system_path(system),
        )
        return None if multi and serial is None else stub

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
            _warn("redfish.resource_skipped", resource=key, path=path, error=str(exc))
            return None
        else:
            return members

    async def _drives(
        self, client: Any, controllers: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """
        Read every drive behind a system's `Storage` controllers.

        Each `Drives[].@odata.id` is followed (ADR-0016, 2026-09-13 update).

        Args:
            client (Any): The authenticated client.
            controllers (list[dict[str, Any]] | None): The system's `Storage`
                members, or None when that collection could not be read.

        Returns:
            list[dict[str, Any]] | None: Every drive, deduplicated by
                `@odata.id`, or None if storage could not be read at all or
                any listed drive could not (the stored drives are kept).
        """
        if controllers is None:
            return None
        drives: dict[str, dict[str, Any]] = {}
        incomplete = False
        for controller in controllers:
            for link in controller.get("Drives", []) or []:
                if not isinstance(link, dict):
                    continue
                try:
                    path = validate_odata_id(link.get("@odata.id"))
                    if path not in drives:
                        drives[path] = await client.get(path)
                except (RedfishForbiddenError, RedfishProtocolError) as exc:
                    incomplete = True
                    _warn("redfish.drive_skipped", resource=link.get("@odata.id"), error=str(exc))
        # A drive that cannot be read must not vanish from the list: a short list overwrites the
        # stored one, so a failed drive would drop out of the record and the server would heal.
        return None if incomplete else list(drives.values())

    async def _storage_collection(
        self, client: Any, system: dict[str, Any]
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """
        Read the system's `Storage` collection, telling "this BMC has none" from "try again".

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The owning `ComputerSystem`.

        Returns:
            tuple[list[dict[str, Any]] | None, str]: The members and `ok`, or None and `absent`
                (no link, or 404/501/503 on an old BMC with no storage schema) or `failed`
                (a timeout, 403 or any other error, kept unread).
        """
        link = system.get("Storage")
        path = link.get("@odata.id") if isinstance(link, dict) else None
        if not path:
            return None, raid.ABSENT_TREE
        try:
            members: list[dict[str, Any]] = await client.get_collection(validate_odata_id(path))
        except RedfishProtocolError as exc:
            _warn("redfish.resource_skipped", resource="Storage", path=path, error=str(exc))
            absent = exc.status_code in _TREE_ABSENT_STATUSES
            return None, raid.ABSENT_TREE if absent else raid.FAILED
        except (RedfishForbiddenError, RedfishUnreachableError) as exc:
            _warn("redfish.resource_skipped", resource="Storage", path=path, error=str(exc))
            return None, raid.FAILED
        return members, raid.OK

    async def _raid(
        self,
        client: Any,
        system: dict[str, Any],
        members: list[dict[str, Any]] | None,
        storage_status: str,
    ) -> tuple[tuple[dict[str, object], ...] | None, dict[str, object] | None]:
        """
        Read every RAID source this BMC offers and decide the OS-mirror verdict (ADR-0048).

        Reads each `Storage` member's `Volumes`, and for an HPE BMC also the OEM
        `SmartStorage` tree (iLO 5 below fw 2.30 and iLO 4 keep RAID only there).

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The owning `ComputerSystem`.
            members (list[dict[str, Any]] | None): The `Storage` members, when read.
            storage_status (str): `ok`, `absent` or `failed` for that read.

        Returns:
            tuple[tuple[dict[str, object], ...] | None, dict[str, object] | None]: The
                controllers for display and `assess_raid`'s verdict. Either is None when a
                source failed to read, so a failure never reads as "no RAID".
        """
        vendor = vendor_from_manufacturer(system.get("Manufacturer")).value
        label = raid.bmc_label(client.service_root, system)
        volumes: dict[str, list[dict[str, Any]]] = {}
        complete = True
        for member in members or []:
            key = str(member.get("@odata.id") or "")
            link = member.get("Volumes")
            path = link.get("@odata.id") if isinstance(link, dict) else None
            if not path:
                volumes[key] = []
                continue
            try:
                volumes[key] = await client.get_collection(validate_odata_id(path))
            except (RedfishForbiddenError, RedfishProtocolError, RedfishUnreachableError) as exc:
                _warn("redfish.volumes_skipped", resource=path, controller=key, error=str(exc))
                complete = False
        smart_status, smart = raid.ABSENT_TREE, []
        oem_raw = system.get("Oem")
        oem = oem_raw if isinstance(oem_raw, dict) else {}
        if vendor == "hp" or "Hpe" in oem or "Hp" in oem:
            smart_status, smart = await self._smart_storage(client, system)
        logical_drives = [ld for c in smart for ld in c["logical_drives"]]
        verdict = raid.assess_raid(
            vendor,
            storage_status=storage_status,
            controllers=members,
            volumes_by_controller=volumes,
            volumes_complete=complete,
            smart_status=smart_status,
            smart_logical_drives=logical_drives,
            smart_controller_count=len(smart),
            bmc=label,
        )
        readable = complete and smart_status != raid.FAILED
        controllers = (
            controllers_from_storage(members, volumes, vendor, smart)
            if readable and members is not None
            else None
        )
        if controllers is None and readable and smart:
            controllers = controllers_from_storage([], {}, vendor, smart)
        return controllers, verdict

    async def _smart_storage(
        self, client: Any, system: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Read HPE's OEM `SmartStorage` array controllers and their logical drives.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The owning `ComputerSystem`.

        Returns:
            tuple[str, list[dict[str, Any]]]: `ok` with `{"controller", "logical_drives"}`
                entries, `absent` when the tree does not exist (no link, 404/501/503), or
                `failed` with no entries on any other error.
        """
        oem = system.get("Oem")
        hpe = (oem.get("Hpe") or oem.get("Hp")) if isinstance(oem, dict) else None
        links = hpe.get("Links") if isinstance(hpe, dict) else None
        smart_link = links.get("SmartStorage") if isinstance(links, dict) else None
        base = smart_link.get("@odata.id") if isinstance(smart_link, dict) else None
        base = base or f"{system.get('@odata.id')}/SmartStorage"
        try:
            root = await client.get(validate_odata_id(base))
            root_links = root.get("Links")
            array_link = (
                root_links.get("ArrayControllers") if isinstance(root_links, dict) else None
            )
            array_path = array_link.get("@odata.id") if isinstance(array_link, dict) else None
            arrays = await client.get_collection(
                validate_odata_id(array_path or f"{base}/ArrayControllers")
            )
            found: list[dict[str, Any]] = []
            for array in arrays:
                array_links = array.get("Links")
                ld_link = (
                    array_links.get("LogicalDrives") if isinstance(array_links, dict) else None
                )
                ld_path = ld_link.get("@odata.id") if isinstance(ld_link, dict) else None
                drives = await client.get_collection(
                    validate_odata_id(ld_path or f"{array.get('@odata.id')}/LogicalDrives")
                )
                found.append({"controller": array, "logical_drives": drives})
        except RedfishProtocolError as exc:
            _warn("redfish.smartstorage_skipped", resource=base, error=str(exc))
            absent = exc.status_code in _TREE_ABSENT_STATUSES
            return (raid.ABSENT_TREE if absent else raid.FAILED), []
        except (RedfishForbiddenError, RedfishUnreachableError) as exc:
            _warn("redfish.smartstorage_skipped", resource=base, error=str(exc))
            return raid.FAILED, []
        return raid.OK, found

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
            _warn("redfish.resource_skipped", resource=key, path=path, error=str(exc))
            return None
        else:
            return resolved

    async def _chassis(self, client: Any, system: dict[str, Any]) -> dict[str, Any] | None:
        """
        Read a system's owning `Chassis` through its first `Links.Chassis` entry.

        Shared by the PSU, PCIe-GPU-fallback and Model-fallback reads,
        which all start from the same link (ADR-0016, 2026-09-16 update).

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The `ComputerSystem` to start from.

        Returns:
            dict[str, Any] | None: The chassis, or None when there is no
                link or it could not be read.
        """
        links = system.get("Links", {})
        chassis_refs = links.get("Chassis") if isinstance(links, dict) else None
        if not isinstance(chassis_refs, list) or not chassis_refs:
            return None
        if not isinstance(chassis_refs[0], dict):
            return None
        try:
            return await client.get(validate_odata_id(chassis_refs[0].get("@odata.id")))
        except (RedfishForbiddenError, RedfishProtocolError, RedfishUnreachableError) as exc:
            _warn("redfish.resource_skipped", resource="Chassis", error=str(exc))
            return None

    async def _chassis_fallback(self, client: Any, system: dict[str, Any]) -> dict[str, Any] | None:
        """
        The owning `Chassis`, fetched only when `Model` or `SerialNumber` needs it.

        Skipped when both are already usable, so a normal host costs no
        extra request — confirmed live, ADR-0016's 2026-09-16 update.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The `ComputerSystem` to start from.

        Returns:
            dict[str, Any] | None: The chassis, or None when neither
                field needs a fallback, there is no chassis link, or it
                could not be read.
        """
        model = system.get("Model")
        model_usable = isinstance(model, str) and bool(model.strip())
        serial_usable = (
            _dell_serial(system) is not None
            or _clean_serial(system.get("SerialNumber")) is not None
        )
        if model_usable and serial_usable:
            return None
        return await self._chassis(client, system)

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
        chassis = await self._chassis(client, system)
        if chassis is None:
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

    async def _psu_telemetry(
        self, client: Any, supplies: list[dict[str, Any]] | None
    ) -> dict[str, dict[str, Any]]:
        """
        Read each PSU's own `PowerSupplyMetrics` for its real-time input power draw.

        See ADR-0016's 2026-09-15 PSU telemetry update.

        Args:
            client (Any): The authenticated client.
            supplies (list[dict[str, Any]] | None): `PowerSupply`
                resources, or None when unread — nothing to look up.

        Returns:
            dict[str, dict[str, Any]]: Each `PowerSupplyMetrics` reached,
                keyed by its owning supply's `@odata.id`. The deprecated
                `Power` resource has no such link, so a supply reached
                that way contributes nothing here.
        """
        metrics: dict[str, dict[str, Any]] = {}
        for supply in supplies or ():
            supply_id = str(supply.get("@odata.id") or "")
            if not supply_id:
                continue
            fetched = await self._optional_link(client, supply, "Metrics")
            if fetched is not None:
                metrics[supply_id] = fetched
        return metrics

    async def _pcie_gpus(
        self, client: Any, system: dict[str, Any]
    ) -> tuple[dict[str, object], ...] | None:
        """
        Screen a system's `PCIeDevices` for GPUs its `Processors` did not report.

        Two real, distinct shapes exist for where the references live —
        see ADR-0016's 2026-09-15 update.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The `ComputerSystem` to start from.

        Returns:
            tuple[dict[str, object], ...] | None: GPUs found, or None
                when neither shape is present or both fail to read.
        """
        max_devices = self._pcie_gpu_max_devices
        found: dict[str, dict[str, Any]] = {}
        truncated = False

        direct_refs = pcie_device_refs(system)
        if direct_refs:
            for ref in direct_refs[:max_devices]:
                try:
                    device = await client.get(f"{validate_odata_id(ref)}?{_PCIE_SELECT}")
                except (
                    RedfishForbiddenError,
                    RedfishProtocolError,
                    RedfishUnreachableError,
                ) as exc:
                    _warn("redfish.resource_skipped", resource=ref, error=str(exc))
                    continue
                found[str(device.get("@odata.id") or ref)] = device
            truncated = truncated or len(direct_refs) > max_devices

        remaining = max_devices - len(found)
        if remaining > 0:
            chassis = await self._chassis(client, system)
            if chassis is not None:
                link = chassis.get("PCIeDevices")
                path = link.get("@odata.id") if isinstance(link, dict) else None
                if path:
                    try:
                        collected, collection_truncated = await self._paged_members(
                            client, validate_odata_id(path), max_members=remaining
                        )
                    except (
                        RedfishForbiddenError,
                        RedfishProtocolError,
                        RedfishUnreachableError,
                    ) as exc:
                        _warn("redfish.resource_skipped", resource=path, error=str(exc))
                    else:
                        for device in collected:
                            key = str(device.get("@odata.id") or "")
                            if key:
                                found[key] = device
                        truncated = truncated or collection_truncated

        if not found and not direct_refs:
            return None
        if truncated:
            _warn("redfish.pcie_scan_truncated", limit=max_devices)
        return gpus_from_pcie_devices(
            list(found.values()),
            max_gpus=self._pcie_gpu_max_gpus,
            pci_device_models=self._pcie_gpu_models,
        )

    async def _paged_members(
        self, client: Any, path: str, *, max_members: int, allow_expand: bool = True
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        Page a collection up to `max_members`, using `$expand` when the BMC advertises it.

        See ADR-0016's 2026-09-15 PCIeDevice update.

        Args:
            client (Any): The authenticated client.
            path (str): The collection's own `@odata.id`, already validated.
            max_members (int): Stop once this many members are collected.
            allow_expand (bool): False retries a collection whose first
                `$expand`ed page came back suspiciously empty.

        Returns:
            tuple[list[dict[str, Any]], bool]: The members (each a full
                resource body), and whether the collection held more
                than `max_members` and was cut off.
        """
        query = _pcie_scan_query(client.service_root) if allow_expand else f"?{_PCIE_SELECT}"
        expanding = allow_expand and "$expand" in query
        members: list[dict[str, Any]] = []
        next_path: str | None = path
        first_page = True
        while next_path and len(members) < max_members:
            page = await client.get(f"{next_path}{query}" if query else next_path)
            page_members = page.get("Members", []) or []
            if first_page and expanding and not page_members:
                advertised = page.get("Members@odata.count")
                if isinstance(advertised, int) and advertised > 0:
                    _warn(
                        "redfish.pcie_expand_returned_nothing",
                        resource=path,
                        path=path,
                        advertised=advertised,
                    )
                    return await self._paged_members(
                        client, path, max_members=max_members, allow_expand=False
                    )
            first_page = False
            for member in page_members:
                if len(members) >= max_members:
                    break
                if not isinstance(member, dict):
                    continue
                if "@odata.type" in member:
                    members.append(member)
                else:
                    # Not pre-expanded — $select alone narrows this one
                    # resource's body; re-requesting $expand on a single
                    # member (rather than the collection) buys nothing.
                    ref = validate_odata_id(member.get("@odata.id"))
                    members.append(await client.get(f"{ref}?{_PCIE_SELECT}"))
            raw_next = page.get("Members@odata.nextLink")
            next_path = validate_odata_id(raw_next) if raw_next else None
        return members, next_path is not None

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
        managed_by = links.get("ManagedBy") if isinstance(links, dict) else None
        if not isinstance(managed_by, list) or not managed_by:
            return None
        if not isinstance(managed_by[0], dict):
            return None
        with contextlib.suppress(RedfishError):
            manager = await client.get(validate_odata_id(managed_by[0].get("@odata.id")))
            interfaces = await self._optional(client, manager, "EthernetInterfaces")
            for interface in interfaces or []:
                mac = interface.get("PermanentMACAddress") or interface.get("MACAddress")
                if isinstance(mac, str) and mac.strip():
                    return mac.strip()
        return None
