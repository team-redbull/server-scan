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
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager, ManagerRun
from app.domain.ports.credentials import (
    CredentialResolver,
    ManagerConnection,
    ManagerNotConfiguredError,
)
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
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
from app.infrastructure.providers.intersight.provider import IntersightProvider
from app.infrastructure.providers.oneview.provider import OneViewProvider
from app.infrastructure.providers.openmanage.provider import OpenManageProvider
from app.infrastructure.providers.redfish.provider import (
    AUTH_REJECTED_MARKER,
    UNREACHABLE_MARKER,
    RedfishStandaloneProvider,
)
from app.infrastructure.providers.redfish.targets import (
    RedfishCredential,
    RedfishTarget,
    load_targets,
)
from app.infrastructure.providers.ucs_central.provider import UcsCentralProvider
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

_DEBUG_HTTP_VAR = "INVENTORY_REDFISH_DEBUG_HTTP"


def _debug_http_enabled() -> bool:
    """
    Report whether `--debug-http` was passed.

    Threaded through the environment rather than the factory signature so
    it reaches every client the run constructs, matching how `--debug-xml`
    already reaches `UcsManagerClient`.

    Returns:
        bool: True when HTTP tracing is on.
    """
    return os.environ.get(_DEBUG_HTTP_VAR) == "1"


def _openmanage_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """
    Build the Dell collector: OME says who exists, each iDRAC says what it is.

    See `PROVIDER_FACTORIES`' comment below.

    Args:
        manager (Manager): The `Manager` projection for `OPENMANAGE`.
        credentials (ManagerConnection): The OME appliance login.
        timeout_seconds (float): Per-call timeout passed to the provider.
        settings (Settings): Process-wide settings, for the BMC login and
            Redfish tuning knobs.
        name_pattern (str): The resolved name filter for this collector,
            from `resolve_name_pattern`.

    Returns:
        ServerInventoryProvider: The Dell collector.

    Raises:
        ManagerNotConfiguredError: When the shared iDRAC login
            (`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD`) is not set.
    """
    # Raised here, before any connection is attempted, so a half-configured
    # deployment gets the variable names to set rather than a per-BMC 401
    # that reads like a fleet of bad passwords. `_run` turns
    # `ManagerNotConfiguredError` into exit code 2 with this message.
    if not settings.ome_bmc_username or not settings.ome_bmc_password:
        raise ManagerNotConfiguredError(
            "The Dell collector reads hardware from each server's iDRAC over "
            "Redfish, so it needs a BMC login as well as the OME appliance "
            "login. Set INVENTORY_OME_BMC_USERNAME and "
            "INVENTORY_OME_BMC_PASSWORD. See "
            "docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md."
        )
    bmc_credential = RedfishCredential(
        # Named, not anonymous: this string is what the Redfish collector's
        # auth guard and every log line report in place of the secret.
        name="ome-bmc",
        username=settings.ome_bmc_username,
        # `ome_bmc_password` is a `SecretStr` — unwrapped here because
        # `RedfishCredential.password` is a plain `str` consumed directly
        # in an HTTP login body. `str(settings.ome_bmc_password)` would
        # silently sign every iDRAC login with the literal `"**********"`.
        password=settings.ome_bmc_password.get_secret_value(),
    )

    def redfish_for(targets: list[RedfishTarget]) -> ServerInventoryProvider:
        """
        Build the hardware pass over the BMCs OME just named.

        Args:
            targets (list[RedfishTarget]): The discovered fleet.

        Returns:
            ServerInventoryProvider: The Redfish collector, tuned by the
                shared `redfish_*` settings — same protocol, same class of
                device, so deliberately not a second set of knobs.
        """
        return RedfishStandaloneProvider(
            manager=manager,
            targets=targets,
            connect_timeout=settings.redfish_connect_timeout_seconds,
            read_timeout=settings.redfish_read_timeout_seconds,
            host_budget_seconds=settings.redfish_host_budget_seconds,
            run_budget_seconds=settings.redfish_run_budget_seconds,
            fleet_concurrency=settings.redfish_fleet_concurrency,
            tls_min_version=settings.redfish_tls_min_version,
            debug_http=_debug_http_enabled(),
        )

    return OpenManageProvider(
        manager=manager,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        bmc_credential=bmc_credential,
        redfish_provider_factory=redfish_for,
        # Applied before any BMC is contacted: the expensive pass here is
        # per-server, not per-appliance. The authoritative name filter is
        # still `_NameFilteredProvider`.
        name_pattern=name_pattern,
        bmc_port=settings.ome_bmc_port,
        bmc_verify_tls=settings.ome_bmc_verify_tls,
        bmc_verify_tls_reason=(
            None
            if settings.ome_bmc_verify_tls
            else (
                "INVENTORY_OME_BMC_VERIFY_TLS is off: iDRACs ship a factory self-signed certificate"
            )
        ),
        bmc_ca_bundle=settings.redfish_ca_bundle or None,
    )


def _ucs_central_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """
    Build the Cisco collector: Central names the domains, each domain's own UCS Manager for servers.

    See `PROVIDER_FACTORIES`' comment below.

    Args:
        manager (Manager): The `Manager` projection for `UCS_CENTRAL`.
        credentials (ManagerConnection): The UCS Central login.
        timeout_seconds (float): Per-call timeout passed to the provider.
        settings (Settings): Process-wide settings, for the per-domain
            UCS Manager login and concurrency.
        name_pattern (str): The resolved name filter for this collector,
            from `resolve_name_pattern`.

    Returns:
        ServerInventoryProvider: The UCS Central collector.

    Raises:
        ManagerNotConfiguredError: When the fleet-wide UCS Manager login
            (`INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD`) is not set.
    """
    # Raised here, before any connection is attempted, so a half-configured
    # deployment gets the variable names to set rather than a per-domain
    # login failure that reads like bad credentials. `_run` turns
    # `ManagerNotConfiguredError` into exit code 2 with this message.
    domain_login = resolve_login(settings, ManagerType.UCS_MANAGER)
    return UcsCentralProvider(
        manager=manager,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        domain_login=domain_login,
        # The same pattern `_NameFilteredProvider` applies, reused only to
        # skip domains that certainly hold nothing of ours. It never
        # decides which *servers* are ingested — see `domains_to_collect`.
        name_pattern=name_pattern,
        concurrency=settings.ucs_central_domain_concurrency,
    )


# One entry per manager type this tool can be pointed at. Every type now
# has one except UCS_MANAGER, whose absence is deliberate and explained
# below; a future type without one gets a clear "not implemented yet"
# from `_build_provider` rather than being silently skipped.
#
# Cisco has exactly one entry point, and it is UCS_CENTRAL:
#
#   UCS_CENTRAL  Central enumerates the registered domains and their
#                service-profile names; each domain's real inventory is
#                then read live from that domain's own UCS Manager through
#                the emulator-validated `..ucs_manager` path. One Central
#                login (INVENTORY_UCS_CENTRAL_*) plus one UCS Manager login
#                valid fleet-wide (INVENTORY_UCS_MANAGER_USERNAME/
#                _PASSWORD). There is no INVENTORY_UCS_MANAGER_IP: Central
#                supplies each domain's address.
#
# UCS_MANAGER is deliberately absent. `UcsManagerProvider` is not gone —
# it is the engine this collector drives once per domain — but it has no
# endpoint of its own to be configured with any more, so pointing this
# tool at UCS_MANAGER is a usage error rather than a missing feature, and
# `_build_provider` says so in its own words instead of claiming no
# collector exists.
#
# REDFISH_STANDALONE is the one entry that names no manager at all: it
# reaches machines no aggregator owns, one BMC at a time, from an
# inventory file. See docs/adr/0016-redfish-standalone-collector.md.
#
# Annotated rather than inferred: mypy would otherwise widen the value
# type, and the annotation is also what pins the factory contract every
# future provider must match.
def _redfish_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """The standalone Redfish collector — one BMC at a time, from a file.

    `name_pattern` is deliberately unused: this collector has no cheap
    pre-filter to spend it on — every host in the inventory file is
    contacted before its name is known. `_NameFilteredProvider` still
    applies whatever `resolve_name_pattern` returned.

    `timeout_seconds` is deliberately unused. This collector splits
    connect from read (`INVENTORY_REDFISH_CONNECT_TIMEOUT_SECONDS` /
    `_READ_TIMEOUT_SECONDS`), because one value cannot serve both a fleet
    with dead hosts in it and a BMC that answers slowly. Named here so a
    parameter this factory ignores is documented rather than surprising.
    """
    return RedfishStandaloneProvider(
        manager=manager,
        # Parsed and fully validated before any connection is opened: a
        # fan-out collector must not find a typo on host 380 of 400, and a
        # credential must never reach a host a mistake put in front of.
        targets=load_targets(
            inventory_path=settings.redfish_inventory_file,
            credentials_path=settings.redfish_credentials_file,
            fallback_login=_optional_login(settings, ManagerType.REDFISH_STANDALONE),
            ca_bundle=settings.redfish_ca_bundle or None,
        ),
        connect_timeout=settings.redfish_connect_timeout_seconds,
        read_timeout=settings.redfish_read_timeout_seconds,
        host_budget_seconds=settings.redfish_host_budget_seconds,
        run_budget_seconds=settings.redfish_run_budget_seconds,
        fleet_concurrency=settings.redfish_fleet_concurrency,
        tls_min_version=settings.redfish_tls_min_version,
        debug_http=_debug_http_enabled(),
    )


def _intersight_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """The Intersight collector — one endpoint, fleet-wide list queries.

    `name_pattern` is deliberately unused: every sub-resource is listed
    once for the whole estate and joined in memory, so there is no
    per-server cost a name filter could avoid paying.
    `_NameFilteredProvider` still applies it.

    `timeout_seconds` is the connect timeout only. Reading one page of a
    fleet-wide query is a different question from reaching the endpoint
    at all, so it has its own setting, the same split the Redfish
    collector makes.
    """
    modes = tuple(
        mode.strip() for mode in settings.intersight_management_modes.split(",") if mode.strip()
    )
    # `ManagerConnection` carries a username/password pair because most
    # vendors have one. Intersight does not: the resolver filled those two
    # slots from INVENTORY_INTERSIGHT_API_KEY_ID/_PEM, and they are named
    # for what they are from here on.
    return IntersightProvider(
        manager=manager,
        endpoint=credentials.endpoint,
        api_key_id=credentials.username,
        api_key_pem=credentials.password,
        connect_timeout=timeout_seconds,
        read_timeout=settings.intersight_read_timeout_seconds,
        page_size=settings.intersight_page_size,
        management_modes=modes,
        run_budget_seconds=settings.intersight_run_budget_seconds,
        debug_http=_debug_http_enabled(),
    )


def _oneview_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """The HPE collector — OneView for every server, whatever its iLO.

    One endpoint and one login, like every other vendor here. There is
    no BMC login and no Redfish pass: a deliberate decision for a mixed
    iLO 4/5/6 fleet, recorded in
    docs/adr/0022-oneview-only-hpe-collector.md.
    """
    return OneViewProvider(
        manager=manager,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        # Applied to the *profile* name, which is the only place a HPE
        # server's operator-assigned name exists. Still only an
        # efficiency gate; `_NameFilteredProvider` remains authoritative
        # — but it does decide which servers cost a `/powerSupplies` or
        # `/processors` call, the two per-server costs this collector has.
        name_pattern=name_pattern,
        page_size=settings.oneview_page_size,
        collect_psus=settings.oneview_collect_psus,
        psu_concurrency=settings.oneview_psu_concurrency,
        collect_cpu_threads=settings.oneview_collect_cpu_threads,
        cpu_threads_concurrency=settings.oneview_cpu_threads_concurrency,
        api_version=settings.oneview_api_version,
        verify_tls=settings.oneview_verify_tls,
    )


def _optional_login(settings: Settings, manager_type: ManagerType) -> tuple[str, str] | None:
    """A type's fleet-wide login, or None when it has none configured.

    Unlike `resolve_login`, a missing value is not an error here: an
    estate where every BMC has its own account configures no fleet-wide
    fallback at all, and `load_targets` then produces a far better message
    naming the specific host whose credential could not be resolved.
    """
    try:
        return resolve_login(settings, manager_type)
    except ManagerNotConfiguredError:
        return None


# Types reached without a single configured endpoint. Both resolve a
# login only; their addresses come from elsewhere at runtime — UCS Central
# reports its domains, and the Redfish collector reads an inventory file.
# `EnvConnectionResolver.resolve` would otherwise demand an `_IP` variable
# that deliberately does not exist.
_ENDPOINTLESS_TYPES = frozenset({ManagerType.REDFISH_STANDALONE})

# The *global* `INVENTORY_COLLECTOR_NAME_PATTERN` is not applied to these
# (a per-type override still is). The pattern exists because a vendor
# manager holds the whole datacenter and the name is the only
# discriminator; a standalone collector's inventory file is already that
# filter, and a far more precise one. Applying `^ocp` over a name a BMC
# does not know would discard every host the operator listed.
_UNFILTERED_TYPES = frozenset({ManagerType.REDFISH_STANDALONE})

# manager type -> the `Settings` field overriding `collector_name_pattern`
# for that collector alone, reusing each type's own env prefix
# (`INVENTORY_ONEVIEW_NAME_PATTERN`, …). Explicit rather than derived from
# the member name, for the reason `..credentials.env`'s maps are.
_NAME_PATTERN_FIELD: dict[ManagerType, str] = {
    ManagerType.UCS_CENTRAL: "ucs_central_name_pattern",
    ManagerType.INTERSIGHT: "intersight_name_pattern",
    ManagerType.OPENMANAGE: "ome_name_pattern",
    ManagerType.ONEVIEW: "oneview_name_pattern",
    ManagerType.REDFISH_STANDALONE: "redfish_name_pattern",
}


def resolve_name_pattern(manager_type: ManagerType, settings: Settings) -> str:
    """
    The name filter one collector actually runs with.

    The single place the global, the per-type override and
    `_UNFILTERED_TYPES` are reconciled — every reader goes through it, so
    the authoritative `_NameFilteredProvider` and the collectors' own
    pruning gates cannot end up filtering on different patterns and
    silently collecting the intersection.

    Args:
        manager_type (ManagerType): Which collector is being run.
        settings (Settings): The settings to resolve from.

    Returns:
        str: The regex to filter server names with; empty means no filter.
    """
    field = _NAME_PATTERN_FIELD.get(manager_type)
    override = getattr(settings, field) if field is not None else None
    if override is not None:
        return override
    if manager_type in _UNFILTERED_TYPES:
        return ""
    return settings.collector_name_pattern


# The single source of truth for "which collectors exist". Public, and
# read across the language boundary by
# `tests/unit/test_frontend_manager_types.py`, because the alternative —
# each consumer restating the same list by hand — is what let the Dell
# and HPE collectors ship unfilterable in the UI while the guard written
# to catch exactly that drifted along with them and stayed green.
PROVIDER_FACTORIES: dict[ManagerType, Callable[..., ServerInventoryProvider]] = {
    ManagerType.UCS_CENTRAL: _ucs_central_provider,
    ManagerType.OPENMANAGE: _openmanage_provider,
    ManagerType.REDFISH_STANDALONE: _redfish_provider,
    # INTERSIGHT is in neither `_ENDPOINTLESS_TYPES` nor
    # `_UNFILTERED_TYPES`, deliberately: it has a real configured
    # endpoint, and the servers it reports carry the `ocp4-...` names
    # `INVENTORY_COLLECTOR_NAME_PATTERN` exists to filter. See
    # docs/adr/0017-intersight-collector.md.
    ManagerType.INTERSIGHT: _intersight_provider,
    # ONEVIEW is ordinary: one endpoint, one login, and the name
    # pattern applies, so it is in neither `_ENDPOINTLESS_TYPES` nor
    # `_UNFILTERED_TYPES`. See
    # docs/adr/0022-oneview-only-hpe-collector.md.
    ManagerType.ONEVIEW: _oneview_provider,
}


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


def manager_for(manager_type: ManagerType, connection: ManagerConnection) -> Manager:
    """
    Build the `Manager` document representing this deployment's single manager of `manager_type`.

    Derived from configuration rather than read from MongoDB: with one
    endpoint per vendor type, a stored document would be a second copy of
    what the environment already says, free to drift from it. The id is
    deterministic so re-running a collector updates the same document
    instead of accumulating one per run, and so every server it ingests
    keeps a stable `manager_id`.

    It is still written to the `managers` collection on each run — the API
    and UI resolve `Server.manager_id` through it — but as a projection of
    config, never as its source.

    Args:
        manager_type (ManagerType): Which vendor manager type this is for.
        connection (ManagerConnection): The resolved endpoint to record.

    Returns:
        Manager: The projection to write and pass to `IngestService`.
    """
    return Manager(
        id=f"mgr_{manager_type.value.lower()}",
        name=manager_type.value.lower().replace("_", "-"),
        type=manager_type,
        endpoint=connection.endpoint,
        enabled=True,
        audit=AuditFields.new(),
    )


class _NameFilteredProvider(ServerInventoryProvider):
    """
    Drop every server whose name doesn't match `pattern` before it reaches the pipeline.

    Implements `INVENTORY_COLLECTOR_NAME_PATTERN` and its per-collector
    overrides, as reconciled by `resolve_name_pattern`.

    A wrapper here rather than a guard inside `IngestService` because
    *which servers to collect* is a collection concern: it belongs to the
    thing pointed at a vendor manager holding the whole datacenter, not
    to the pipeline shared with `tools/seed_inventory.py`, whose fake
    servers have no manager to be filtered out of. Wrapping also keeps
    `--dry-run` honest — it bypasses `IngestService` on purpose, so a
    filter living there would make a dry run print servers a real run
    would never write.

    Vendor-agnostic by construction: it wraps `ServerInventoryProvider`,
    so every real collector inherits it without a line of their own.

    A wrapper, not a vendor: `collection_errors` is overridden to
    delegate to `_inner`'s own rather than the base's — it never calls
    `_record_error` itself, so accumulating into the inherited list would
    silently under-report (a filtered run always reads back empty).
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

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield only the wrapped collector's servers whose name matches `pattern`.

        Yields:
            ProviderServer: Each server from `inner` that passed the
                name filter.
        """
        kept = skipped = 0
        # `aclosing`, because this wrapper sits in front of *every*
        # collector: a consumer stopping early (`--dry-run --limit`)
        # throws `GeneratorExit` in at the `yield` below, and without this
        # the inner provider's own teardown — cancelling its host/domain
        # tasks, logging out of its sessions — waited on the asyncgen
        # finalizer instead of running now.
        async with contextlib.aclosing(self._inner.collect()) as servers:
            async for provider_server in servers:
                if self._pattern.search(provider_server.name):
                    kept += 1
                    yield provider_server
                else:
                    skipped += 1
        # Logged unconditionally, including the all-zero case: "0 kept, 0
        # skipped" is the signature of a wrong endpoint, while "0 kept,
        # 900 skipped" is the signature of a wrong pattern, and an
        # otherwise-successful empty run looks identical without it.
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


def _build_provider(
    manager: Manager,
    *,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    settings: Settings | None = None,
) -> ServerInventoryProvider:
    """
    Build the provider for `manager.type` via `PROVIDER_FACTORIES`.

    `settings` is threaded in rather than read here so a caller (and a
    test) can decide which collector variant it is exercising. It defaults
    to the process-wide settings for the callers that have no opinion.

    Args:
        manager (Manager): The manager to build a provider for.
        credential_resolver (CredentialResolver): Resolves the login or
            API key for `manager.type`.
        timeout_seconds (float): Per-call timeout passed to the provider.
        settings (Settings | None): Falls back to `get_settings()` when
            omitted.

    Returns:
        ServerInventoryProvider: The constructed collector.

    Raises:
        NotImplementedError: When `manager.type` has no factory in
            `PROVIDER_FACTORIES` (`UCS_MANAGER` gets its own message
            explaining it is reached through `UCS_CENTRAL` instead).
    """
    factory = PROVIDER_FACTORIES.get(manager.type)
    if factory is None:
        # UCS Manager gets its own message: the collector for it very much
        # exists and runs on every Central run, it just has no endpoint of
        # its own to be pointed at. "Not implemented yet" would send an
        # operator looking for code that is already there.
        if manager.type is ManagerType.UCS_MANAGER:
            raise NotImplementedError(
                "UCS Manager has no collector entry point of its own — it is collected "
                "through `--manager-type UCS_CENTRAL`, which discovers every registered "
                "domain's address from UCS Central and logs into each one with "
                "INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD."
            )
        raise NotImplementedError(
            f"No collector implemented yet for manager type {manager.type!r}."
        )
    connection = (
        ManagerConnection(endpoint="", username="", password="")
        if manager.type in _ENDPOINTLESS_TYPES
        else credential_resolver.resolve(manager.type)
    )
    resolved = settings if settings is not None else get_settings()
    return factory(
        manager=manager,
        credentials=connection,
        timeout_seconds=timeout_seconds,
        settings=resolved,
        # Threaded in rather than each factory reading `Settings` again:
        # a collector's own pruning gate and `_NameFilteredProvider` must
        # be the same pattern, or a run collects their intersection.
        name_pattern=resolve_name_pattern(manager.type, resolved),
    )


# What a dry run prints for a field the provider could not read, as
# distinct from `—` for a field it read and found empty. See
# `app.domain.ports.provider.ProviderServer`.
_UNREAD = "not read"


def _bmc_host(address_raw: str | None) -> str:
    """
    The BMC's address as an operator reads it: host only.

    The scheme, port and Redfish path a collector reports are protocol
    detail nobody checks by eye, and they push the one thing that matters
    — the address you would ping or open — off to the middle of a line.
    The full URI is still what gets stored, for the Metal3 `BareMetalHost`
    round-trip.

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
    Render a byte count as GiB, or TiB once GiB stops being readable at a glance.

    Binary (base-1024), and kept for GPU VRAM specifically: Redfish reports
    it in MiB, so a 80 GiB card reads as its nameplate size only in binary
    units. Disk capacity uses `_format_tb`/`_format_disk_size` instead —
    drives are marketed and reported decimal.

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
    Render one drive's capacity in the unit its model is marketed in.

    Decimal (base-1000), switching to TB at 1 TB so a drive reads the way
    its model names it — a `480GB` drive as GB, a `1.92TB` drive as TB —
    rather than a 1.92 TB disk showing as `1920.0 GB`.

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


# Neither an unreachable host nor a rejected credential marks the CronJob
# pod failed any more — added 2026-09-10 after real OME runs.
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
    Print what `manager` reports, writing nothing.

    Deliberately bypasses `IngestService` entirely rather than passing it
    some no-op repository: the point of a dry run is to see what the
    *provider* produces, before classification, health evaluation and
    correlation have had a chance to reshape it. Anything printed here is
    the raw `ProviderServer` the collector would hand to the pipeline.

    Timed end to end (provider construction through the last server),
    wrapped in `finally` so a run that fails partway still reports how
    long it took before dying — see `collector.run_complete` below.

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
            # A plain `async for` always fetches its *next* item before the
            # loop body ever runs, so checking the limit at the bottom of the
            # loop (below) — which is what stops this from pulling one server
            # past every other `--limit N` — cannot also cover `N == 0`
            # without ever entering the loop, and therefore the provider,
            # at all. Handled here instead of inside it.
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
                # Service profiles and fabric attachments are UCS concepts. A
                # provider with neither (OpenManage, Redfish) printed "— " and "0"
                # on every server, which reads as missing data rather than as a
                # field its vendor has no equivalent for.
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
                        # Fabric-interconnect identity only makes sense on a
                        # cabled physical uplink. A vNIC (`HostEthInterface` on
                        # Intersight, `vnic` on UCS Manager/Central) structurally
                        # never carries a fabric relationship at all — printing
                        # "fabric None / FI model/serial=—/—" on every vNIC line
                        # reads as missing data rather than as a field its kind
                        # has no equivalent for. This is also why a standalone
                        # server (which contributes zero PHYSICAL attachments —
                        # nothing to cable to a Fabric Interconnect it doesn't
                        # have) never shows an FI-shaped line at all: the
                        # attachment fabric() already skips an uncabled physical
                        # interface, so every one of its attachments is VNIC.
                        # See docs/cisco-collectors.md, "PHYSICAL versus VNIC".
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
                # Per-NIC detail for the providers that report it — Redfish, and so
                # the Dell collector that delegates to it. The flat `nic macs` line
                # above is all a provider without it has.
                for nic in ps.nics:
                    # `is not None`, not truthiness: a BMC that reports a
                    # real `0` (an explicitly-down link some firmware
                    # reports that way) is a read, not an absence, and
                    # dashes the same way every other unread field here
                    # does rather than disappearing silently.
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
                    # Redfish only, and there for the same reason `power=`
                    # below is: the raw `Status.Health`/`Status.State`
                    # pair, so a live run can settle whether mapping
                    # Warning to UNKNOWN rather than DOWN is right before
                    # that becomes a CRITICAL finding. Empty for every
                    # provider that doesn't report it.
                    redfish_status = psu.get("redfish_status")
                    status = f"  status={redfish_status}" if redfish_status else ""
                    print(
                        f"        psu {psu.get('id')}  {psu.get('model') or '—'}"
                        f"  serial={psu.get('serial') or '—'}"
                        f"  {f'{capacity}W' if isinstance(capacity, int) else 'wattage unknown'}"
                        f"  health={psu.get('health')} ({psu.get('health_detail') or '—'})"
                        # UCS Manager only: the equipmentPsu MO's separate `power`
                        # field, collected alongside oper_state so a live run can
                        # show which one tracks a real PSU failure more reliably
                        # before this settles on one (docs/cisco-collectors.md,
                        # "Power supplies (PSUs)"). Always "—" for a provider that
                        # doesn't report it, Intersight included.
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
        # `seconds` stays a raw float — a dashboard graphs it, and a
        # formatted string would break that. `took` is the same duration
        # a second time, formatted, for whoever is scanning the log by
        # eye rather than piping it into a metrics pipeline.
        logger.info(
            "collector.run_complete",
            dry_run=True,
            fetched=count,
            seconds=duration,
            took=_format_duration(duration),
        )


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """What one manager's run produced, and what it could not reach.

    `summary` alone cannot express a partial run: a domain that failed
    contributes no servers and no ingest errors, so its absence is
    invisible in the counts.

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
        # No explicit `provider.health_check()` here: `IngestService.
        # ingest()` already calls it as its first step, and a UCS login is
        # ~4 sequential HTTP round trips (auth, then the SDK's own
        # is-this-UCSM / version / domain-name probes), so calling it here
        # too would double that cost per manager and burn a second session
        # against UCS Manager's per-user session cap for nothing — this
        # `except` handles a health-check failure identically either way.
        # `managers=[manager]` is what actually writes the `Manager`
        # projection `manager_for()` builds. Without it every collected
        # server carried a `manager_id` pointing at a document that was
        # never created — see docs/adr/0016.
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
    # Bound once for the whole run so every log line from here down —
    # including `ingest.completed`, deep inside `IngestService.ingest`,
    # which has no `manager_type` field of its own to log — carries which
    # collector produced it, without threading the value through every
    # intervening call.
    structlog.contextvars.bind_contextvars(manager_type=manager_type.value)

    try:
        credential_resolver = EnvConnectionResolver(settings)

        # One manager per type, straight from configuration — nothing is
        # read from the `managers` collection to decide where to connect.
        try:
            if manager_type in _ENDPOINTLESS_TYPES:
                # No `_IP` variable exists for these, so `resolve()` would
                # exit 2 naming a variable that cannot be set. The
                # `Manager` projection carries the inventory path instead,
                # which is the most informative answer to "where did these
                # servers come from".
                connection = ManagerConnection(
                    endpoint=settings.redfish_inventory_file,
                    username="",
                    password="",
                )
            else:
                connection = credential_resolver.resolve(manager_type)
            # Pre-flight for the one collector that needs a *second* set of
            # credentials: UCS Central also logs into each domain's UCS
            # Manager. Checked here, beside the endpoint resolution, so a
            # half-configured deployment exits 2 naming the variables to set
            # — the provider factory raises the same error later, but by
            # then the dry-run/ingest paths have turned it into a generic
            # "FAILED (see logs)" exit 1.
            if manager_type is ManagerType.UCS_CENTRAL:
                resolve_login(settings, ManagerType.UCS_MANAGER)
        except ManagerNotConfiguredError as exc:
            logger.exception("collector.not_configured", manager_type=manager_type.value)
            print(f"{exc}")
            return 2
        manager = manager_for(manager_type, connection)
        name_pattern = resolve_name_pattern(manager_type, settings)

        if dry_run:
            # No indexes, no ingest pipeline, no repositories at all.
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

        # Everything above needs no MongoDB connection at all: `--dry-run`
        # only talks to the vendor manager, never to the database, and
        # connecting here unconditionally used to mean a misconfigured or
        # unreachable Mongo could fail a dry run that was never going to
        # touch it.
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
            # Timed here, around the call, rather than inside
            # `_run_one_manager` itself: that function already swallows a
            # failed run into `None` (so the CronJob doesn't die on one
            # bad manager), and `finally` below is what makes a run that
            # fails partway still report how long it took before dying —
            # exactly the run most worth seeing the duration of.
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
            # A dead BMC or a rejected credential no longer makes the run
            # PARTIAL — see `_is_benign_collection_error`.
            benign_count = len(outcome.collection_errors) - len(hard_errors)
            if partial:
                # Exit 3, not 0: some servers were written, but this run did
                # not see the whole fleet, for a reason worth a human
                # looking at across many hosts (TLS, a per-host budget, an
                # unrecognized error) — not just one dead BMC or credential,
                # which `_is_benign_collection_error` already excluded above.
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
        # Read by `UcsManagerClient`; set here so it covers every provider
        # this run constructs.
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
