"""How a `ManagerType` becomes a constructed `ServerInventoryProvider`.

The one place that decision lives, for both callers: `tools.run_collector`
(a CronJob run) and `GET /api/v1/servers/available`'s live recheck
(ADR-0032). Moved here from `tools/run_collector.py` on 2026-09-13 so the
API never imports the CLI layer above it — `tools/` is not an installed
package, and `uvicorn --app-dir backend` could not see it.

See docs/architecture.md, "How tools/run_collector.py is put together".
"""

from __future__ import annotations

import os
from collections.abc import Callable

from app.config import get_settings
from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import (
    CredentialResolver,
    ManagerConnection,
    ManagerNotConfiguredError,
)
from app.domain.ports.provider import ServerInventoryProvider
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.credentials.env import resolve_login
from app.infrastructure.providers.intersight.provider import IntersightProvider
from app.infrastructure.providers.oneview.provider import OneViewProvider
from app.infrastructure.providers.openmanage.provider import OpenManageProvider
from app.infrastructure.providers.redfish.provider import RedfishStandaloneProvider
from app.infrastructure.providers.redfish.targets import (
    RedfishCredential,
    RedfishTarget,
    load_targets,
)
from app.infrastructure.providers.ucs_central.provider import UcsCentralProvider

DEBUG_HTTP_VAR = "INVENTORY_REDFISH_DEBUG_HTTP"


def debug_http_enabled() -> bool:
    """
    Report whether `--debug-http` was passed.

    Via the environment, so it reaches every client the run constructs.

    Returns:
        bool: True when HTTP tracing is on.
    """
    return os.environ.get(DEBUG_HTTP_VAR) == "1"


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

    See docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md.

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
    if not settings.ome_bmc_username or not settings.ome_bmc_password:
        raise ManagerNotConfiguredError(
            "The Dell collector reads hardware from each server's iDRAC over "
            "Redfish, so it needs a BMC login as well as the OME appliance "
            "login. Set INVENTORY_OME_BMC_USERNAME and "
            "INVENTORY_OME_BMC_PASSWORD. See "
            "docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md."
        )
    bmc_credential = RedfishCredential(
        name="ome-bmc",
        username=settings.ome_bmc_username,
        # `get_secret_value()`, not `str()`: the latter would sign every
        # iDRAC login with the literal `"**********"`.
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
            debug_http=debug_http_enabled(),
            pcie_gpu_detection=settings.redfish_pcie_gpu_detection,
            pcie_gpu_max_devices=settings.redfish_pcie_gpu_max_devices,
            pcie_gpu_max_gpus=settings.redfish_pcie_gpu_max_gpus,
        )

    return OpenManageProvider(
        manager=manager,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        bmc_credential=bmc_credential,
        redfish_provider_factory=redfish_for,
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

    See docs/adr/0014-ucs-central-multi-domain-collector.md.

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
    domain_login = resolve_login(settings, ManagerType.UCS_MANAGER)
    return UcsCentralProvider(
        manager=manager,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        domain_login=domain_login,
        name_pattern=name_pattern,
        concurrency=settings.ucs_central_domain_concurrency,
    )


def _redfish_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """
    Build the standalone Redfish collector: one BMC at a time, from a file.

    See docs/adr/0016-redfish-standalone-collector.md.

    Args:
        manager (Manager): The `Manager` projection for `REDFISH_STANDALONE`.
        credentials (ManagerConnection): Unused — logins come from the
            credentials file, with `INVENTORY_REDFISH_*` as the fallback.
        timeout_seconds (float): Unused — connect and read are split
            (`INVENTORY_REDFISH_CONNECT_TIMEOUT_SECONDS`/`_READ_TIMEOUT_SECONDS`).
        settings (Settings): Process-wide settings, for the Redfish knobs.
        name_pattern (str): Unused — every host is contacted before its
            name is known; `_NameFilteredProvider` still applies it.

    Returns:
        ServerInventoryProvider: The standalone Redfish collector.
    """
    return RedfishStandaloneProvider(
        manager=manager,
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
        debug_http=debug_http_enabled(),
        pcie_gpu_detection=settings.redfish_pcie_gpu_detection,
        pcie_gpu_max_devices=settings.redfish_pcie_gpu_max_devices,
        pcie_gpu_max_gpus=settings.redfish_pcie_gpu_max_gpus,
    )


def _intersight_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """
    Build the Intersight collector: one endpoint, fleet-wide list queries.

    See docs/adr/0017-intersight-collector.md.

    Args:
        manager (Manager): The `Manager` projection for `INTERSIGHT`.
        credentials (ManagerConnection): The API key — the resolver put
            `INVENTORY_INTERSIGHT_API_KEY_ID`/`_PEM` in the username and
            password slots.
        timeout_seconds (float): The connect timeout only; a fleet-wide
            page read has its own `INVENTORY_INTERSIGHT_READ_TIMEOUT_SECONDS`.
        settings (Settings): Process-wide settings, for the Intersight knobs.
        name_pattern (str): Unused — every sub-resource is listed once for
            the whole estate, so there is no per-server cost to skip;
            `_NameFilteredProvider` still applies it.

    Returns:
        ServerInventoryProvider: The Intersight collector.
    """
    modes = tuple(
        mode.strip() for mode in settings.intersight_management_modes.split(",") if mode.strip()
    )
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
        debug_http=debug_http_enabled(),
    )


def _oneview_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
    name_pattern: str,
) -> ServerInventoryProvider:
    """
    Build the HPE collector: OneView for every server, whatever its iLO.

    No BMC login, no Redfish pass — docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        manager (Manager): The `Manager` projection for `ONEVIEW`.
        credentials (ManagerConnection): The OneView appliance login.
        timeout_seconds (float): Per-call timeout passed to the provider.
        settings (Settings): Process-wide settings, for the OneView knobs.
        name_pattern (str): The resolved name filter, applied to the
            profile name to skip the per-server `/powerSupplies` and
            `/processors` calls; `_NameFilteredProvider` stays authoritative.

    Returns:
        ServerInventoryProvider: The HPE collector.
    """
    return OneViewProvider(
        manager=manager,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
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
    """
    A type's fleet-wide login, or None when none is configured.

    Not an error: `load_targets` names the specific host left without one.

    Args:
        settings (Settings): The settings to resolve from.
        manager_type (ManagerType): Whose login to resolve.

    Returns:
        tuple[str, str] | None: `(username, password)`, or None.
    """
    try:
        return resolve_login(settings, manager_type)
    except ManagerNotConfiguredError:
        return None


# See docs/architecture.md, "How tools/run_collector.py is put together".
ENDPOINTLESS_TYPES = frozenset({ManagerType.REDFISH_STANDALONE})

UNFILTERED_TYPES = frozenset({ManagerType.REDFISH_STANDALONE})

NAME_PATTERN_FIELD: dict[ManagerType, str] = {
    ManagerType.UCS_CENTRAL: "ucs_central_name_pattern",
    ManagerType.INTERSIGHT: "intersight_name_pattern",
    ManagerType.OPENMANAGE: "ome_name_pattern",
    ManagerType.ONEVIEW: "oneview_name_pattern",
    ManagerType.REDFISH_STANDALONE: "redfish_name_pattern",
}


def resolve_name_pattern(manager_type: ManagerType, settings: Settings) -> str:
    """
    The name filter one collector actually runs with.

    The single place the global, the per-type override and `UNFILTERED_TYPES`
    are reconciled, so the wrapper and a collector's own pruning agree.

    Args:
        manager_type (ManagerType): Which collector is being run.
        settings (Settings): The settings to resolve from.

    Returns:
        str: The regex to filter server names with; empty means no filter.
    """
    field = NAME_PATTERN_FIELD.get(manager_type)
    override = getattr(settings, field) if field is not None else None
    if override is not None:
        return override
    if manager_type in UNFILTERED_TYPES:
        return ""
    return settings.collector_name_pattern


# Which collectors exist; `UCS_MANAGER` is deliberately absent (docs/architecture.md).
PROVIDER_FACTORIES: dict[ManagerType, Callable[..., ServerInventoryProvider]] = {
    ManagerType.UCS_CENTRAL: _ucs_central_provider,
    ManagerType.OPENMANAGE: _openmanage_provider,
    ManagerType.REDFISH_STANDALONE: _redfish_provider,
    ManagerType.INTERSIGHT: _intersight_provider,
    ManagerType.ONEVIEW: _oneview_provider,
}


def build_provider_for_manager_type(
    manager_type: ManagerType, *, settings: Settings
) -> ServerInventoryProvider:
    """
    Build a provider for one manager type, for a live single-server recheck.

    `_run()`'s own resolution, minus its dry-run/reporting concerns (ADR-0032).

    Args:
        manager_type (ManagerType): Which collector to build.
        settings (Settings): Process-wide settings to resolve credentials
            from.

    Returns:
        ServerInventoryProvider: The constructed collector, unwrapped (no
            name filter — `get_one()` correlates by identity, not by name).

    Raises:
        ManagerNotConfiguredError: This manager type has no connection
            configured on this process, naming the missing variable(s).
        NotImplementedError: `manager_type` has no entry in
            `PROVIDER_FACTORIES` (`UCS_MANAGER`, reached only through
            `UCS_CENTRAL`).
    """
    credential_resolver = EnvConnectionResolver(settings)
    if manager_type in ENDPOINTLESS_TYPES:
        connection = ManagerConnection(
            endpoint=settings.redfish_inventory_file, username="", password=""
        )
    else:
        connection = credential_resolver.resolve(manager_type)
    if manager_type is ManagerType.UCS_CENTRAL:
        resolve_login(settings, ManagerType.UCS_MANAGER)
    manager = manager_for(manager_type, connection)
    return build_provider(
        manager,
        credential_resolver=credential_resolver,
        timeout_seconds=settings.collector_connect_timeout_seconds,
        settings=settings,
    )


def manager_for(manager_type: ManagerType, connection: ManagerConnection) -> Manager:
    """
    Build the `Manager` document for this deployment's manager of `manager_type`.

    A projection of configuration with a deterministic id, never read back
    to decide where to connect — see docs/architecture.md.

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


def build_provider(
    manager: Manager,
    *,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    settings: Settings | None = None,
) -> ServerInventoryProvider:
    """
    Build the provider for `manager.type` via `PROVIDER_FACTORIES`.

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
        # A usage error, not a missing feature — see docs/architecture.md.
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
        if manager.type in ENDPOINTLESS_TYPES
        else credential_resolver.resolve(manager.type)
    )
    resolved = settings if settings is not None else get_settings()
    return factory(
        manager=manager,
        credentials=connection,
        timeout_seconds=timeout_seconds,
        settings=resolved,
        name_pattern=resolve_name_pattern(manager.type, resolved),
    )
