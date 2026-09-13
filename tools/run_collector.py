"""CLI: run a real vendor collector against every enabled `Manager` of a given type.

Ingests whatever it reports through the exact same `IngestService` pipeline
`tools/seed_inventory.py` exercises with fake data (classify ->
health-evaluate -> audit -> upsert, in one write).

This is what a Kubernetes `CronJob` actually invokes — one CronJob per
manager type (`--manager-type UCS_MANAGER`, etc.), matching how the
platform's own `Manager` documents are already partitioned. A manager
failing (unreachable, bad credentials, an unexpected API response) is
logged and counted but never aborts the run for the *other* managers of
the same type — one flaky UCS domain shouldn't block ingesting the
other nine.

Usage:
    uv run python -m tools.run_collector --manager-type UCS_MANAGER
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import datetime

import structlog

from app.application.services.audit_service import AuditService
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService, IngestSummary
from app.config import get_settings
from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.models.manager import Manager, ManagerRun
from app.domain.ports.credentials import (
    CredentialResolver,
    ManagerConnection,
    ManagerNotConfiguredError,
)
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.bmc_address import parse_bmc_address
from app.domain.value_objects.gpu_catalog import gpu_catalog
from app.domain.value_objects.site import parse_site_code, site_catalog
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.credentials.env import resolve_login
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.factory import (
    DEBUG_HTTP_VAR,
    ENDPOINTLESS_TYPES,
    PROVIDER_FACTORIES,
    build_provider,
    build_provider_for_manager_type,
    manager_for,
    resolve_name_pattern,
)
from app.infrastructure.providers.redfish.provider import (
    AUTH_REJECTED_MARKER,
    UNREACHABLE_MARKER,
)
from app.utils.timeutil import utcnow

# Construction moved to `app.infrastructure.providers.factory` (ADR-0032);
# the old names stay importable so the tests that drive this CLI still do.
_DEBUG_HTTP_VAR = DEBUG_HTTP_VAR
_ENDPOINTLESS_TYPES = ENDPOINTLESS_TYPES
_build_provider = build_provider

__all__ = [
    "PROVIDER_FACTORIES",
    "build_provider_for_manager_type",
    "main",
    "manager_for",
    "resolve_name_pattern",
]

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse this CLI's arguments.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: The parsed `--manager-type`/`--dry-run`/
            `--debug-http`/`--debug-xml`/`--limit` values.
    """
    parser = argparse.ArgumentParser(
        description="Run a real vendor collector against every enabled Manager of one type."
    )
    parser.add_argument(
        "--manager-type",
        required=True,
        choices=sorted(ManagerType.__members__),
        help="Only Manager documents of this type are collected from.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print what each manager reports and exit without writing anything. "
            "Nothing is classified, health-evaluated, audited or upserted."
        ),
    )
    parser.add_argument(
        "--debug-http",
        action="store_true",
        help=(
            "Log one line per Redfish request: method, path and status only. "
            "Headers and bodies are never logged, and the session exchange is "
            "skipped entirely rather than redacted."
        ),
    )
    parser.add_argument(
        "--debug-xml",
        action="store_true",
        help=(
            "Dump every XML request and response ucsmsdk exchanges with the "
            "manager. Very verbose — pair it with --dry-run --limit."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="With --dry-run, stop after N servers per manager.",
    )
    return parser.parse_args(argv)


class _NameFilteredProvider(ServerInventoryProvider):
    """
    Drop every server whose name doesn't match `pattern` before the pipeline.

    `INVENTORY_COLLECTOR_NAME_PATTERN` and its per-collector overrides, as
    a wrapper rather than an `IngestService` guard — see docs/architecture.md.
    """

    def __init__(self, inner: ServerInventoryProvider, pattern: str) -> None:
        """
        Wrap `inner`, keeping only servers whose name matches `pattern`.

        Args:
            inner (ServerInventoryProvider): The real collector to wrap.
            pattern (str): A regex; a server is kept when this matches
                somewhere in its name.
        """
        super().__init__()
        self._inner = inner
        self._pattern = re.compile(pattern)
        self.provider_type = inner.provider_type

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """Pass the wrapped collector's partial failures straight through.

        Without this the wrapper would swallow them and every filtered run
        would look complete.
        """
        return self._inner.collection_errors

    async def health_check(self) -> None:
        """Delegate to the wrapped collector's own health check."""
        await self._inner.health_check()

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """Delegate to the wrapped collector — a live recheck isn't name-filtered."""
        return await self._inner.get_one(identity)

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield only the wrapped collector's servers whose name matches `pattern`.

        Yields:
            ProviderServer: Each server from `inner` that passed the
                name filter.
        """
        kept = skipped = 0
        async with contextlib.aclosing(self._inner.collect()) as servers:
            async for provider_server in servers:
                if self._pattern.search(provider_server.name):
                    kept += 1
                    yield provider_server
                else:
                    skipped += 1
        logger.info(
            "collector.name_filter_applied",
            pattern=self._pattern.pattern,
            kept=kept,
            skipped=skipped,
        )


def _filtered(provider: ServerInventoryProvider, pattern: str) -> ServerInventoryProvider:
    """
    Wrap `provider` in `_NameFilteredProvider`, or return it unwrapped when there is no pattern.

    Args:
        provider (ServerInventoryProvider): The collector to filter.
        pattern (str): The name-matching regex; empty means no filtering.

    Returns:
        ServerInventoryProvider: `provider` itself when `pattern` is
            empty, otherwise a `_NameFilteredProvider` wrapping it.
    """
    return _NameFilteredProvider(provider, pattern) if pattern else provider


# A field the provider could not read, as distinct from `—` for read-and-empty.
_UNREAD = "not read"


def _bmc_host(address_raw: str | None) -> str:
    """
    The BMC's address as an operator reads it: host only.

    The scheme, port and Redfish path are still stored in full.

    Args:
        address_raw (str | None): The collector's raw BMC address.

    Returns:
        str: The host, the raw string when it cannot be parsed, or an em
            dash when there is none.
    """
    parsed = parse_bmc_address(address_raw)
    if parsed is None:
        return "—"
    return parsed.host or parsed.raw


def _or_unread(value: object) -> str:
    """
    Render an optionally-reported value for the dry-run print.

    Args:
        value (object): The reported value, or `None` if the provider
            could not read it.

    Returns:
        str: The value as text, or `"not read"` for `None`.
    """
    return _UNREAD if value is None else str(value)


def _format_capacity(capacity_bytes: int) -> str:
    """
    Render a byte count as binary GiB/TiB.

    For GPU VRAM, which Redfish reports in MiB; drives are decimal and
    use `_format_disk_size`.

    Args:
        capacity_bytes (int): The capacity to render, in bytes.

    Returns:
        str: e.g. `"512.0 GiB"` below 1024 GiB, `"9.6 TiB"` at or above it.
    """
    gib = capacity_bytes / 1024**3
    if gib >= 1024:
        return f"{gib / 1024:.1f} TiB"
    return f"{gib:.1f} GiB"


def _format_tb(capacity_bytes: int) -> str:
    """
    Render a byte count as decimal TB.

    Decimal (base-1000), matching how storage is marketed and reported, and
    used for a whole server's storage total.

    Args:
        capacity_bytes (int): The capacity to render, in bytes.

    Returns:
        str: e.g. `"10.56 TB"`.
    """
    return f"{capacity_bytes / 1000**4:.2f} TB"


def _format_disk_size(capacity_bytes: int) -> str:
    """
    Render one drive's capacity in the decimal unit its model is marketed in.

    A `480GB` drive as GB, a `1.92TB` drive as TB.

    Args:
        capacity_bytes (int): The capacity to render, in bytes.

    Returns:
        str: e.g. `"480.0 GB"` below 1 TB, `"1.92 TB"` at or above it.
    """
    if capacity_bytes >= 1000**4:
        return f"{capacity_bytes / 1000**4:.2f} TB"
    return f"{capacity_bytes / 1000**3:.1f} GB"


def _format_speed(speed_mbps: int) -> str:
    """
    Render a NIC's link speed for the CLI dry-run print.

    Args:
        speed_mbps (int): The reported speed, in Mbps
            (`ProviderNic.speed_mbps`).

    Returns:
        str: e.g. `"100 Mbps"` below 1000 Mbps, `"25 Gbps"` at or above
            it — `:g` drops a trailing `.0` (`1000` -> `"1 Gbps"`) but
            keeps a real fraction (`2500` -> `"2.5 Gbps"`).
    """
    if speed_mbps >= 1000:
        return f"{speed_mbps / 1000:g} Gbps"
    return f"{speed_mbps} Mbps"


def _format_duration(seconds: float) -> str:
    """
    Render a run's wall-clock duration for the CLI summary line.

    Args:
        seconds (float): Elapsed time, in seconds.

    Returns:
        str: e.g. `"42.3s"` below a minute, `"2m 13s"` at or above it.
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder}s"


_BENIGN_COLLECTION_ERROR_MARKERS = (
    UNREACHABLE_MARKER,
    AUTH_REJECTED_MARKER,
)


def _is_benign_collection_error(message: str) -> bool:
    """
    Whether a `collection_errors` entry must not make the run PARTIAL.

    Args:
        message (str): One entry from `ProviderServer.collection_errors`.

    Returns:
        bool: `True` for a plain unreachable host or a rejected login.
            `False` for everything else (TLS, budget-exceeded, a generic
            `RedfishError`) — those can still signal a problem worth a
            human looking at across many hosts.
    """
    return any(marker in message for marker in _BENIGN_COLLECTION_ERROR_MARKERS)


async def _record_run(
    manager_repo: MongoManagerRepository,
    manager_id: str,
    *,
    started_at: datetime,
    duration: float,
    summary: IngestSummary,
    collection_errors: int,
    partial: bool,
) -> None:
    """
    Write the run's outcome onto its Manager document, for the fleet gauges (ADR-0029).

    Never raises: the exit code must reflect the collection, not this write.

    Args:
        manager_repo (MongoManagerRepository): Where the record goes.
        manager_id (str): The manager this run was for.
        started_at (datetime): When the run began.
        duration (float): Wall-clock seconds the run took.
        summary (IngestSummary): What ingestion reported.
        collection_errors (int): Hosts the provider could not collect.
        partial (bool): Whether the run will exit 3.
    """
    run = ManagerRun(
        started_at=started_at,
        finished_at=utcnow(),
        duration_seconds=duration,
        servers_fetched=summary.fetched,
        servers_created=summary.created,
        servers_updated=summary.updated,
        ingest_errors=summary.errors,
        collection_errors=collection_errors,
        partial=partial,
    )
    try:
        await manager_repo.record_run(manager_id, run)
    except Exception as exc:
        logger.warning("collector.record_run_failed", manager_id=manager_id, error=str(exc))


async def _dry_run_one_manager(
    manager: Manager,
    *,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    limit: int | None,
    name_pattern: str = "",
    settings: Settings | None = None,
    provider_factory: Callable[..., ServerInventoryProvider] | None = None,
) -> int:
    """
    Print the raw `ProviderServer`s `manager` reports, writing nothing.

    Bypasses `IngestService` entirely so what is printed is what the
    provider produced, before classification or correlation reshape it.

    Args:
        manager (Manager): The manager to collect from.
        credential_resolver (CredentialResolver): Resolves the login or
            API key for `manager`'s type.
        timeout_seconds (float): Per-call timeout passed to the provider.
        limit (int | None): Stop after this many servers; `None` for no
            limit, `0` to report zero without connecting past construction.
        name_pattern (str): Only report servers whose name matches this
            regex; empty matches everything.
        settings (Settings | None): Falls back to `get_settings()` when
            omitted.
        provider_factory (Callable[..., ServerInventoryProvider] | None):
            Overrides `_build_provider`; a test seam.

    Returns:
        int: How many servers were reported.
    """
    start = time.monotonic()
    count = 0
    try:
        build = provider_factory or _build_provider
        provider = _filtered(
            build(
                manager,
                credential_resolver=credential_resolver,
                timeout_seconds=timeout_seconds,
                settings=settings,
            ),
            name_pattern,
        )
        sites = site_catalog(settings.sites if settings is not None else get_settings().sites)
        gpus_catalog = gpu_catalog(
            settings.gpu_models if settings is not None else get_settings().gpu_models
        )
        print(f"\n=== {manager.name} ({manager.type.value} @ {manager.endpoint}) ===")
        if name_pattern:
            print(f"    (only servers whose name matches {name_pattern!r} are shown/collected)")

        if limit == 0:
            # `async for` would fetch one server before the bottom-of-loop check.
            print("  … stopped at --limit 0")
            return count
        async with contextlib.aclosing(provider.collect()) as servers:
            async for ps in servers:
                count += 1
                site = parse_site_code(ps.name, sites) or parse_site_code(ps.profile_dn, sites)
                memory = (
                    f"{ps.memory_total_bytes / 1024**3:.1f} GiB"
                    if ps.memory_total_bytes is not None
                    else _UNREAD
                )
                storage = (
                    _format_tb(ps.storage_total_bytes)
                    if ps.storage_total_bytes is not None
                    else _UNREAD
                )
                drive_count = _or_unread(
                    None if ps.storage_drives is None else len(ps.storage_drives)
                )
                macs = _UNREAD if ps.nic_macs is None else (", ".join(ps.nic_macs) or "—")
                profile = f"\n     profile     : {ps.profile_dn}" if ps.profile_dn else ""
                attachments = (
                    f"\n     attachments : {len(ps.attachments)}" if ps.attachments else ""
                )
                print(
                    f"\n[{count}] {ps.name}"
                    f"\n     external_id : {ps.external_id}"
                    f"\n     site (from name): {site or '— none in name'}"
                    f"\n     vendor/model: {ps.vendor} / {ps.model}"
                    f"\n     serial/uuid : {ps.serial} / {_or_unread(ps.system_uuid)}"
                    f"\n     cpu         : {_or_unread(ps.cpu_sockets)} sockets,"
                    f" {_or_unread(ps.cpu_cores)} cores,"
                    f" {_or_unread(ps.cpu_threads)} threads ({ps.cpu_model or 'model unknown'})"
                    f"\n     memory      : {memory}"
                    f"\n     storage     : {storage} total across {drive_count} drive(s)"
                    f"\n     bmc         : {_bmc_host(ps.bmc_address_raw)}"
                    f" (mac {ps.bmc_mac or '—'})"
                    f"{profile}"
                    f"\n     profile tmpl: {ps.profile_template_name or '—'}"
                    f" [{ps.profile_template_external_id or '—'}]"
                    f"\n     nic macs    : {macs}"
                    f"{attachments}"
                    f"\n     gpus        : {_or_unread(None if ps.gpus is None else len(ps.gpus))}"
                    f"\n     psus        : {_or_unread(None if ps.psus is None else len(ps.psus))}"
                )
                for a in ps.attachments:
                    if a.interface_kind == "PHYSICAL":
                        # docs/cisco-collectors.md, "PHYSICAL versus VNIC".
                        print(
                            f"        [{a.interface_kind:8}] fabric {a.fabric}"
                            f"  ({a.fabric_name or '—'})"
                            f"  if={a.server_interface}"
                            f"  admin={a.admin_state} oper={a.oper_state}"
                            f"  peer={a.fabric_port or '—'}"
                            f"  FI model/serial={a.fabric_model or '—'}/{a.fabric_serial or '—'}"
                        )
                    else:
                        print(
                            f"        [{a.interface_kind:8}] if={a.server_interface}"
                            f"  admin={a.admin_state} oper={a.oper_state}"
                        )
                for nic in ps.nics:
                    # `is not None`: some firmware reports a down link as a
                    # real `0`, which is a read, not an absence.
                    speed = _format_speed(nic.speed_mbps) if nic.speed_mbps is not None else "—"
                    location = f"  [{nic.location}]" if nic.location else ""
                    print(
                        f"        nic {nic.name}{location}  mac={nic.mac or '—'}"
                        f"  {nic.link_state}  {speed}"
                    )
                for drive in ps.storage_drives or ():
                    capacity_bytes = drive.get("capacity_bytes")
                    size = (
                        _format_disk_size(capacity_bytes)
                        if isinstance(capacity_bytes, int)
                        else "size unknown"
                    )
                    print(
                        f"        disk {drive.get('id')}  {drive.get('model') or '—'}"
                        f"  serial={drive.get('serial') or '—'}"
                        f"  {drive.get('media_type')}  {size}  health={drive.get('health')}"
                        f" ({drive.get('health_detail') or '—'})"
                    )
                for gpu in ps.gpus or ():
                    gpu = gpus_catalog.enrich(gpu)
                    gpu_memory = gpu.get("memory_bytes")
                    gpu_size = (
                        _format_capacity(gpu_memory)
                        if isinstance(gpu_memory, int)
                        else "VRAM unknown"
                    )
                    temp = gpu.get("temperature_celsius")
                    power = gpu.get("power_watts")
                    print(
                        f"        gpu {gpu.get('model') or '—'}  vendor={gpu.get('vendor') or '—'}"
                        f"  serial={gpu.get('serial') or '—'}"
                        f"  {gpu_size} ({gpu.get('memory_type') or 'memory type unknown'})"
                        f"  ecc={gpu.get('ecc_mode_enabled')}"
                        f"  errors={gpu.get('correctable_error_count')}c/"
                        f"{gpu.get('uncorrectable_error_count')}u"
                        f"  temp={f'{temp:.0f}°C' if isinstance(temp, (int, float)) else '—'}"
                        f"  power={f'{power:.0f}W' if isinstance(power, (int, float)) else '—'}"
                        f"  health={gpu.get('health')} ({gpu.get('health_detail') or '—'})"
                    )
                for psu in ps.psus or ():
                    capacity = psu.get("capacity_watts")
                    redfish_status = psu.get("redfish_status")
                    status = f"  status={redfish_status}" if redfish_status else ""
                    print(
                        f"        psu {psu.get('id')}  {psu.get('model') or '—'}"
                        f"  serial={psu.get('serial') or '—'}"
                        f"  {f'{capacity}W' if isinstance(capacity, int) else 'wattage unknown'}"
                        f"  health={psu.get('health')} ({psu.get('health_detail') or '—'})"
                        # UCS Manager only — docs/cisco-collectors.md, "Power supplies (PSUs)".
                        f"  power={psu.get('oper_power') or '—'}"
                        f"{status}"
                    )
                if limit is not None and count >= limit:
                    print(f"  … stopped at --limit {limit}")
                    break
        return count
    finally:
        duration = time.monotonic() - start
        print(
            f"\n{manager.name}: {count} server(s) reported. Nothing was written. "
            f"(took {_format_duration(duration)})"
        )
        logger.info(
            "collector.run_complete",
            dry_run=True,
            fetched=count,
            seconds=duration,
            took=_format_duration(duration),
        )


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """
    What one manager's run produced, and what it could not reach.

    A failed domain contributes no servers and no ingest errors, so
    `summary` alone cannot express a partial run.

    Attributes:
        summary (IngestSummary): Fetched/created/updated/error counts.
        collection_errors (tuple[str, ...]): One message per endpoint the
            collector could not reach, empty for a complete run.
    """

    summary: IngestSummary
    collection_errors: tuple[str, ...]


async def _run_one_manager(
    manager: Manager,
    *,
    ingest_service: IngestService,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    name_pattern: str = "",
    settings: Settings | None = None,
) -> _RunOutcome | None:
    """
    Collect from `manager` and ingest through `ingest_service`, real writes.

    Args:
        manager (Manager): The manager to collect from.
        ingest_service (IngestService): The pipeline to write through.
        credential_resolver (CredentialResolver): Resolves the login or
            API key for `manager`'s type.
        timeout_seconds (float): Per-call timeout passed to the provider.
        name_pattern (str): Only ingest servers whose name matches this
            regex; empty matches everything.
        settings (Settings | None): Falls back to `get_settings()` when
            omitted.

    Returns:
        _RunOutcome | None: The ingest summary and any collection errors,
            or `None` when the manager could not be reached at all (the
            failure is logged, not raised, so one bad manager never
            aborts the run for the others).
    """
    try:
        provider = _filtered(
            _build_provider(
                manager,
                credential_resolver=credential_resolver,
                timeout_seconds=timeout_seconds,
                settings=settings,
            ),
            name_pattern,
        )
        # `ingest()` runs `health_check` itself; `managers=` writes the projection (ADR-0016).
        summary = await ingest_service.ingest(provider, managers=[manager])
        return _RunOutcome(summary=summary, collection_errors=provider.collection_errors)
    except Exception:
        logger.exception(
            "collector.manager_failed", manager_id=manager.id, manager_name=manager.name
        )
        return None


async def _run(
    *, manager_type: ManagerType, dry_run: bool = False, limit: int | None = None
) -> int:
    """
    Run one manager type's collector end to end, real or `--dry-run`.

    Args:
        manager_type (ManagerType): Which collector to run.
        dry_run (bool): Print what the provider reports without writing
            anything, when `True`.
        limit (int | None): Stop after this many servers, or `None` for
            no limit.

    Returns:
        int: Exit code — 0 complete, 1 total failure, 2 not configured,
            3 partial (some servers written, but not the whole fleet).
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    structlog.contextvars.bind_contextvars(manager_type=manager_type.value)

    try:
        credential_resolver = EnvConnectionResolver(settings)

        try:
            if manager_type in _ENDPOINTLESS_TYPES:
                connection = ManagerConnection(
                    endpoint=settings.redfish_inventory_file,
                    username="",
                    password="",
                )
            else:
                connection = credential_resolver.resolve(manager_type)
            # Pre-flight the second login so a missing one exits 2, not 1.
            if manager_type is ManagerType.UCS_CENTRAL:
                resolve_login(settings, ManagerType.UCS_MANAGER)
        except ManagerNotConfiguredError as exc:
            logger.exception("collector.not_configured", manager_type=manager_type.value)
            print(f"{exc}")
            return 2
        manager = manager_for(manager_type, connection)
        name_pattern = resolve_name_pattern(manager_type, settings)

        if dry_run:
            try:
                await _dry_run_one_manager(
                    manager,
                    credential_resolver=credential_resolver,
                    timeout_seconds=settings.collector_connect_timeout_seconds,
                    limit=limit,
                    name_pattern=name_pattern,
                    settings=settings,
                )
            except Exception:
                logger.exception("collector.dry_run_failed", manager_id=manager.id)
                print(f"manager={manager.name} FAILED (see logs)")
                return 1
            return 0

        mongo = MongoClientHolder(settings)
        await mongo.connect()
        try:
            manager_repo = MongoManagerRepository(mongo)
            await ensure_indexes(mongo.db)

            rule_repo = MongoClassificationRuleRepository(mongo)
            policy_repo = MongoHealthPolicyRepository(mongo)
            regex_engine = RegexModuleEngine(
                max_pattern_length=settings.regex_max_pattern_length,
                match_timeout_seconds=settings.regex_match_timeout_seconds,
            )
            server_repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
            ingest_service = IngestService(
                server_repo=server_repo,
                site_repo=MongoSiteRepository(mongo),
                manager_repo=manager_repo,
                sites=site_catalog(settings.sites),
                gpu_catalog=gpu_catalog(settings.gpu_models),
                classification_service=ClassificationService(
                    rule_repo=rule_repo, engine=regex_engine
                ),
                health_service=HealthPolicyService(
                    policy_repo=policy_repo, registry=build_default_registry()
                ),
                audit=AuditService(repo=MongoAuditEventRepository(mongo)),
            )
            run_start = time.monotonic()
            run_started_at = utcnow()
            try:
                outcome = await _run_one_manager(
                    manager,
                    ingest_service=ingest_service,
                    credential_resolver=credential_resolver,
                    timeout_seconds=settings.collector_connect_timeout_seconds,
                    name_pattern=name_pattern,
                    settings=settings,
                )
            finally:
                run_duration = time.monotonic() - run_start

            if outcome is None:
                print(
                    f"manager={manager.name} FAILED (see logs)"
                    f" took={_format_duration(run_duration)}"
                )
                logger.info(
                    "collector.run_complete",
                    dry_run=False,
                    seconds=run_duration,
                    took=_format_duration(run_duration),
                )
                return 1

            summary = outcome.summary
            print(
                f"manager={manager.name} fetched={summary.fetched} "
                f"created={summary.created} updated={summary.updated} errors={summary.errors} "
                f"took={_format_duration(run_duration)}"
            )
            hard_errors = [
                m for m in outcome.collection_errors if not _is_benign_collection_error(m)
            ]
            partial = bool(hard_errors or summary.errors)
            await _record_run(
                manager_repo,
                manager.id,
                started_at=run_started_at,
                duration=run_duration,
                summary=summary,
                collection_errors=len(outcome.collection_errors),
                partial=partial,
            )
            logger.info(
                "collector.run_complete",
                dry_run=False,
                fetched=summary.fetched,
                created=summary.created,
                updated=summary.updated,
                errors=summary.errors,
                seconds=run_duration,
                took=_format_duration(run_duration),
            )
            benign_count = len(outcome.collection_errors) - len(hard_errors)
            if partial:
                logger.error(
                    "collector.partial_run",
                    manager_id=manager.id,
                    unreachable=len(outcome.collection_errors),
                    ingest_errors=summary.errors,
                )
                print(f"manager={manager.name} PARTIAL — this run did not see the whole fleet:")
                for message in outcome.collection_errors:
                    print(f"  - {message}")
                if summary.errors:
                    print(f"  - {summary.errors} server(s) failed to ingest (see logs)")
                return 3
            if benign_count:
                logger.warning(
                    "collector.hosts_skipped",
                    manager_id=manager.id,
                    skipped=benign_count,
                )
                print(
                    f"manager={manager.name} completed — {benign_count} "
                    "host(s) unreachable or auth-rejected (see logs), not counted as PARTIAL:"
                )
                for message in outcome.collection_errors:
                    print(f"  - {message}")
            return 0
        finally:
            await mongo.close()
    finally:
        structlog.contextvars.unbind_contextvars("manager_type")


def main(argv: list[str] | None = None) -> None:
    """
    Entry point: parse args, run `_run`, and exit with its status code.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Raises:
        SystemExit: With `_run`'s exit code — 0 complete, 1 total
            failure, 2 not configured, 3 partial.
    """
    args = _parse_args(argv)
    if args.debug_xml:
        # Read by `UcsManagerClient`.
        os.environ["INVENTORY_UCS_DUMP_XML"] = "1"
    if args.debug_http:
        os.environ[_DEBUG_HTTP_VAR] = "1"
    exit_code = asyncio.run(
        _run(
            manager_type=ManagerType(args.manager_type),
            dry_run=args.dry_run,
            limit=args.limit,
        )
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
