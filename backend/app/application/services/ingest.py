"""
Ingestion pipeline: `ProviderServer` -> domain `Server`, upserted via `ServerRepository`.

Correlation simplification (slice 1 scope, documented here since it's the
one deliberate shortcut in this module): a `ProviderServer` is matched
against an existing document only by `(vendor, serial_normalized)` when a
serial is present; with no serial, it is always treated as a new server.
The full identity-correlation ladder described in
`app.domain.models.server.Identity`'s docstring (system_uuid first, then
BMC MAC, then NIC MACs, then per-manager `external_id`, ...) is a later
slice's job — this keeps ingestion working end-to-end now without
pretending to solve a problem outside this module's scope.

Field ownership: every field this module writes directly is
ingestion-owned (identity, hardware, network, connectivity, name/model,
`source_provider`, `last_seen_at`). It never touches `tags` beyond what
the provider reports, and never touches `maintenance`/`openshift` at all
(those belong to their own future engines) — those two are always carried
forward verbatim from the existing document on update, never reset to
zero. `classification` and `health` are the one exception: when
`classification_service`/`health_service` are supplied (see
`IngestService.__init__`), this module calls them itself, right after
building the rest of the document and before computing `search_tokens`
(which reads `classification.installation_type`) — so a server is
classified and health-evaluated in the same write that ingests it, one
upsert per server rather than a second round-trip. Both services are
optional and default to `None` specifically so ingestion keeps working
before either engine is wired up (and so tests of this module in
isolation don't need to construct them) — with no service supplied, the
previous behavior applies: carry the existing classification/health
forward unchanged, or leave them at their zero value for a new server.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog
from pymongo.errors import DuplicateKeyError

from app.application.services.audit_service import SYSTEM_INGEST_ACTOR, AuditService
from app.application.services.pipeline import classification_from_result, health_from_state
from app.domain.enums import LinkState, ManagerType, MediaType, Vendor
from app.domain.models.audit_event import EventType
from app.domain.models.classification import Classification
from app.domain.models.classification_rule import ClassificationRule
from app.domain.models.connectivity import (
    Connectivity,
    ConnectivityAttachment,
    compute_connectivity_facts,
)
from app.domain.models.hardware import (
    Cpu,
    Gpu,
    Hardware,
    Memory,
    MemoryModule,
    Power,
    Psu,
    Storage,
    StorageDrive,
)
from app.domain.models.health import Health
from app.domain.models.health_policy import HealthPolicy
from app.domain.models.maintenance import Maintenance
from app.domain.models.manager import Manager
from app.domain.models.network import BmcInfo, NetworkInfo, NetworkInterface
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, ProfileTemplate, Server
from app.domain.models.site import Site
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.domain.ports.repository import ServerRepository
from app.domain.services.classification import ClassifiableServer
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.domain.value_objects.bmc_address import parse_bmc_address
from app.domain.value_objects.gpu_catalog import GpuCatalog
from app.domain.value_objects.mac_address import normalize_mac
from app.domain.value_objects.site import SiteCatalog, parse_site_code
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

if TYPE_CHECKING:
    from app.application.services.classification_service import ClassificationService
    from app.application.services.health_policy_service import HealthPolicyService

logger = structlog.get_logger(__name__)


class SiteRepositoryPort(Protocol):
    """
    The one method `IngestService` needs from a site repository.

    Defined here rather than in `app.domain.ports` because it is this
    service's own dependency; `MongoSiteRepository` satisfies it structurally.
    """

    async def upsert(self, site: Site) -> Site:
        """
        Create or update a site document.

        Args:
            site (Site): The site to persist.

        Returns:
            Site: The persisted site.
        """
        ...


class ManagerRepositoryPort(Protocol):
    """The one method `IngestService` needs from a manager repository."""

    async def upsert(self, manager: Manager) -> Manager:
        """
        Create or update a manager document.

        Args:
            manager (Manager): The manager to persist.

        Returns:
            Manager: The persisted manager.
        """
        ...


@dataclass(slots=True)
class IngestSummary:
    """Per-run counters returned by `IngestService.ingest`."""

    fetched: int = 0
    created: int = 0
    updated: int = 0
    errors: int = 0


def _opt_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never intended here
        return int(value)
    if isinstance(value, int):
        return value
    return int(str(value))


def _link_state(value: str) -> LinkState:
    """Map a provider's link-state string onto `LinkState`.

    Args:
        value (str): A `ProviderNic.link_state` value, expected to be one of
            `LinkState`'s members ("UP"/"DOWN"/"DISABLED"/"UNKNOWN").

    Returns:
        LinkState: The matching member, or `LinkState.UNKNOWN` for anything
            a provider reports outside that set.
    """
    try:
        return LinkState(value)
    except ValueError:
        return LinkState.UNKNOWN


def _opt_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _opt_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _carry_forward[T](
    reported: T | None, previous: T | None, *, default: T, unread: list[str], name: str
) -> T:
    """
    Resolve one optionally-reported field against what is already stored.

    `None` means "could not read this run" (ADR-0016): the stored value is
    kept and `name` is appended to `unread`, here, at the one choke point.

    Args:
        reported (T | None): What the provider reported, or `None` if it
            could not read the field.
        previous (T | None): The value on the existing document, or `None`
            for a server being created.
        default (T): The value for a new server whose provider reported
            nothing.
        unread (list[str]): Accumulator for this one ingest, appended to
            whenever `reported` is `None`. Built fresh per server so it
            states what *this* run could not read, never a running total.
        name (str): The field's dotted path in the API response, e.g.
            `"hardware.storage.drives"`.

    Returns:
        T: The reported value when there is one, else the stored value,
            else `default`.
    """
    if reported is not None:
        return reported
    unread.append(name)
    return previous if previous is not None else default


def _gpu_from_dict(data: dict[str, object]) -> Gpu:
    """
    Build a `Gpu` from the untyped dict a provider reports.

    Args:
        data (dict[str, object]): One entry from `ProviderServer.gpus`.

    Returns:
        Gpu: The domain model. `memory_bytes` is already in bytes — the
            provider converts, since Redfish reports GPU memory in MiB.
    """
    return Gpu(
        vendor=_opt_str(data.get("vendor")),
        model=_opt_str(data.get("model")),
        serial=_opt_str(data.get("serial")),
        memory_bytes=_opt_int(data.get("memory_bytes")),
        health=_opt_str(data.get("health")),
        health_detail=_opt_str(data.get("health_detail")),
        pci_address=_opt_str(data.get("pci_address")),
        firmware_version=_opt_str(data.get("firmware_version")),
        memory_type=_opt_str(data.get("memory_type")),
        ecc_mode_enabled=_opt_bool(data.get("ecc_mode_enabled")),
        correctable_error_count=_opt_int(data.get("correctable_error_count")),
        uncorrectable_error_count=_opt_int(data.get("uncorrectable_error_count")),
        temperature_celsius=_opt_float(data.get("temperature_celsius")),
        power_watts=_opt_float(data.get("power_watts")),
    )


def _memory_module_from_dict(data: dict[str, object]) -> MemoryModule:
    """
    Build a `MemoryModule` from the untyped dict a provider reports.

    Args:
        data (dict[str, object]): One entry from
            `ProviderServer.memory_modules`.

    Returns:
        MemoryModule: The domain model. `health` is a `HealthSeverity`
            value, the same vocabulary drives use — not the UP/DOWN one
            PSUs use, because a DIMM's condition is reported as a health
            rollup by every source, never as an operational state.
    """
    return MemoryModule(
        slot=_opt_str(data.get("slot")),
        size_bytes=_opt_int(data.get("size_bytes")),
        type=_opt_str(data.get("type")),
        speed_mhz=_opt_int(data.get("speed_mhz")),
        serial=_opt_str(data.get("serial")),
        health=_opt_str(data.get("health")),
    )


def _psu_from_dict(data: dict[str, object]) -> Psu:
    """
    Build a `Psu` from the untyped dict a provider reports.

    Args:
        data (dict[str, object]): One entry from `ProviderServer.psus`.

    Returns:
        Psu: The domain model.
    """
    return Psu(
        id=str(data.get("id", "")),
        model=_opt_str(data.get("model")),
        serial=_opt_str(data.get("serial")),
        health=_opt_str(data.get("health")),
        health_detail=_opt_str(data.get("health_detail")),
        capacity_watts=_opt_int(data.get("capacity_watts")),
    )


def _drive_from_dict(data: dict[str, object]) -> StorageDrive:
    media_raw = data.get("media_type")
    try:
        media_type = MediaType(str(media_raw)) if media_raw else MediaType.UNKNOWN
    except ValueError:
        media_type = MediaType.UNKNOWN
    return StorageDrive(
        id=str(data.get("id", "")),
        model=_opt_str(data.get("model")),
        serial=_opt_str(data.get("serial")),
        media_type=media_type,
        capacity_bytes=_opt_int(data.get("capacity_bytes")),
        health=_opt_str(data.get("health")),
        health_detail=_opt_str(data.get("health_detail")),
    )


_DEFAULT_GPU_CATALOG = GpuCatalog.from_spec("")


class IngestService:
    """
    Runs a full provider ingest.

    Upserts referenced sites/managers, then normalizes, correlates and
    upserts every `ProviderServer` the provider yields.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        site_repo: SiteRepositoryPort,
        manager_repo: ManagerRepositoryPort,
        sites: SiteCatalog,
        gpu_catalog: GpuCatalog = _DEFAULT_GPU_CATALOG,
        classification_service: ClassificationService | None = None,
        health_service: HealthPolicyService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        """
        Initialize the service with its repositories and optional engines.

        Args:
            server_repo (ServerRepository): The servers collection.
            site_repo (SiteRepositoryPort): Upserts the sites a run references.
            manager_repo (ManagerRepositoryPort): Upserts the managers a run references.
            sites (SiteCatalog): The configured site catalog, used to parse
                each server's site from its name.
            gpu_catalog (GpuCatalog): Fills in GPU VRAM the provider itself
                doesn't report; defaults to the built-in catalog with no
                configured overrides.
            classification_service (ClassificationService | None): When
                given, classifies each server as part of its ingest.
            health_service (HealthPolicyService | None): When given,
                health-evaluates each server as part of its ingest.
            audit (AuditService | None): When given, records
                creation/classification/health transition events.
        """
        self._server_repo = server_repo
        self._sites = sites
        self._gpu_catalog = gpu_catalog
        self._site_repo = site_repo
        self._manager_repo = manager_repo
        self._classification_service = classification_service
        self._health_service = health_service
        self._audit = audit

    async def ingest(
        self,
        provider: ServerInventoryProvider,
        *,
        sites: Sequence[Site] = (),
        managers: Sequence[Manager] = (),
    ) -> IngestSummary:
        """
        Run one full ingest: upsert sites/managers, then every collected server.

        A server-level failure is logged and counted in `IngestSummary.errors`
        rather than aborting the run. See docs/architecture.md, "Ingestion".

        Args:
            provider (ServerInventoryProvider): The vendor provider to collect from.
            sites (Sequence[Site]): Sites to upsert before collecting, idempotently.
            managers (Sequence[Manager]): Managers to upsert before collecting, idempotently.

        Returns:
            IngestSummary: How many servers were fetched, created, updated, and errored.
        """
        summary = IngestSummary()

        for site in sites:
            await self._site_repo.upsert(site)
        for manager in managers:
            await self._manager_repo.upsert(manager)

        await provider.health_check()

        # Once per run, not per server — docs/architecture.md, "Ingestion".
        ruleset = (
            await self._classification_service.load_ruleset()
            if self._classification_service is not None
            else []
        )
        policies = (
            await self._health_service.load_policies() if self._health_service is not None else []
        )

        async for provider_server in provider.collect():
            summary.fetched += 1
            try:
                created = await self._ingest_one(
                    provider_server,
                    provider_type=provider.provider_type,
                    ruleset=ruleset,
                    policies=policies,
                )
            except Exception:
                logger.exception(
                    "ingest.server_failed",
                    external_id=provider_server.external_id,
                    vendor=provider_server.vendor,
                )
                summary.errors += 1
                continue

            if created:
                summary.created += 1
            else:
                summary.updated += 1

        logger.info(
            "ingest.completed",
            fetched=summary.fetched,
            created=summary.created,
            updated=summary.updated,
            errors=summary.errors,
        )
        return summary

    async def _find_by_vendor_serial(self, vendor: Vendor, serial_normalized: str) -> Server | None:
        page = await self._server_repo.list_page(
            filters={
                "identity.vendor": vendor.value,
                "identity.serial_normalized": serial_normalized,
            },
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=1,
            with_count=False,
        )
        return page.items[0] if page.items else None

    async def _ingest_one(
        self,
        ps: ProviderServer,
        *,
        provider_type: str,
        ruleset: list[ClassificationRule],
        policies: list[HealthPolicy],
    ) -> bool:
        """
        Normalize, correlate and upsert one provider record.

        Args:
            ps (ProviderServer): The provider's raw record for one server.
            provider_type (str): The collector's `ManagerType` value.
            ruleset (list[ClassificationRule]): This run's ruleset, loaded
                once by `ingest()` — see `ClassificationService.
                load_ruleset`'s docstring for why.
            policies (list[HealthPolicy]): This run's policy set, loaded
                once by `ingest()` — see `HealthPolicyService.
                load_policies`'s docstring for why.

        Returns:
            bool: True if a new server document was created, False if an
                existing one was updated.
        """
        # No fallback vendor: an unrecognized value is a provider bug to surface.
        try:
            vendor = Vendor(ps.vendor)
        except ValueError as exc:
            raise ValueError(
                f"Provider reported unsupported vendor {ps.vendor!r} for "
                f"{ps.external_id!r}; expected one of {[v.value for v in Vendor]}."
            ) from exc

        serial_normalized = normalize_text(ps.serial)
        existing = (
            await self._find_by_vendor_serial(vendor, serial_normalized)
            if serial_normalized
            else None
        )

        server = await self._build_server(
            ps,
            vendor=vendor,
            serial_normalized=serial_normalized,
            existing=existing,
            provider_type=provider_type,
            ruleset=ruleset,
            policies=policies,
        )

        try:
            await self._server_repo.upsert(server)
        except DuplicateKeyError:
            # A concurrent insert collided on `uniq_vendor_serial`, the only
            # unique index left (ADR-0026): update the real owner in place.
            refetched = (
                await self._find_by_vendor_serial(vendor, serial_normalized)
                if serial_normalized
                else None
            )
            if refetched is None:
                raise
            existing = refetched
            server = await self._build_server(
                ps,
                vendor=vendor,
                serial_normalized=serial_normalized,
                existing=refetched,
                provider_type=provider_type,
                ruleset=ruleset,
                policies=policies,
            )
            await self._server_repo.upsert(server)
            await self._emit_transition_events(existing, server)
            return False

        await self._emit_transition_events(existing, server)
        return existing is None

    async def _emit_transition_events(self, existing: Server | None, server: Server) -> None:
        """
        Audit only the ingestion transitions worth an entry.

        A new server, or an engine verdict that changed — never a generic
        update, which every run would emit for every server.

        Args:
            existing (Server | None): The server as it was before this
                upsert, or `None` if it was just created.
            server (Server): The server as it now stands, already upserted.
        """
        if self._audit is None:
            return

        if existing is None:
            await self._audit.record(
                EventType.SERVER_CREATED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                data={"vendor": server.identity.vendor.value, "name": server.name},
            )
            return

        if server.classification.installation_type != existing.classification.installation_type:
            await self._audit.record(
                EventType.CLASSIFICATION_CHANGED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                data={
                    "from": existing.classification.installation_type.value,
                    "to": server.classification.installation_type.value,
                    "matched_rule_id": server.classification.matched_rule_id,
                },
            )

        if server.health.overall != existing.health.overall:
            await self._audit.record(
                EventType.HEALTH_STATUS_CHANGED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                data={"from": existing.health.overall.value, "to": server.health.overall.value},
            )

    async def _build_server(
        self,
        ps: ProviderServer,
        *,
        vendor: Vendor,
        serial_normalized: str,
        existing: Server | None,
        provider_type: str,
        ruleset: list[ClassificationRule],
        policies: list[HealthPolicy],
    ) -> Server:
        """
        Build the `Server` document for one provider record, without persisting it.

        Args:
            ps (ProviderServer): The provider's raw record for one server.
            vendor (Vendor): `ps.vendor`, already validated and parsed.
            serial_normalized (str): `ps.serial`, normalized for correlation.
            existing (Server | None): The matching stored document, if any.
            provider_type (str): The collector's `ManagerType` value.
            ruleset (list[ClassificationRule]): This run's snapshot, loaded
                once by `ingest()` — see `ClassificationService.
                load_ruleset` for why.
            policies (list[HealthPolicy]): This run's snapshot, loaded once
                by `ingest()` — see `HealthPolicyService.load_policies`
                for why.

        Returns:
            Server: The built document, classified and health-evaluated
                when the corresponding engine was supplied.
        """
        now = utcnow()
        server_id = existing.id if existing is not None else new_id("server")
        created_at = existing.created_at if existing is not None else now
        revision = existing.revision + 1 if existing is not None else 1

        existing_hardware = existing.hardware if existing is not None else None

        # Recomputed every run, never merged — see `Server.unread_fields`.
        unread: list[str] = []

        bmc_parsed = parse_bmc_address(ps.bmc_address_raw)
        bmc_mac = normalize_mac(ps.bmc_mac)
        nic_macs = _carry_forward(
            [mac for mac in (normalize_mac(m) for m in ps.nic_macs) if mac is not None]
            if ps.nic_macs is not None
            else None,
            existing.identity.nic_macs if existing is not None else None,
            default=[],
            unread=unread,
            name="identity.nic_macs",
        )

        identity = Identity(
            vendor=vendor,
            serial=ps.serial,
            serial_normalized=serial_normalized,
            system_uuid=ps.system_uuid,
            nic_macs=nic_macs,
            external_ids={ps.manager_id: ps.external_id} if ps.manager_id else {},
        )
        existing_profile_template = existing.profile_template if existing is not None else None
        profile_template = ProfileTemplate(
            name=_carry_forward(
                ps.profile_template_name,
                existing_profile_template.name if existing_profile_template else None,
                default=None,
                unread=unread,
                name="profile_template.name",
            ),
            external_id=_carry_forward(
                ps.profile_template_external_id,
                existing_profile_template.external_id if existing_profile_template else None,
                default=None,
                unread=unread,
                name="profile_template.external_id",
            ),
        )

        network = NetworkInfo(
            bmc=BmcInfo(
                address_raw=ps.bmc_address_raw,
                scheme=bmc_parsed.scheme if bmc_parsed else None,
                host=bmc_parsed.host if bmc_parsed else None,
                host_is_ip=bmc_parsed.host_is_ip if bmc_parsed else False,
                port=bmc_parsed.port if bmc_parsed else None,
                path=bmc_parsed.path if bmc_parsed else None,
                mac=bmc_mac,
            ),
            interfaces=[
                NetworkInterface(
                    name=nic.name,
                    mac=normalize_mac(nic.mac),
                    speed_mbps=nic.speed_mbps,
                    link_state=_link_state(nic.link_state),
                    location=nic.location,
                )
                for nic in ps.nics
            ],
        )

        attachments = [
            ConnectivityAttachment(
                type=a.type,
                provider=a.provider,
                fabric=a.fabric,
                fabric_name=a.fabric_name,
                fabric_id=a.fabric_id,
                fabric_model=a.fabric_model,
                fabric_serial=a.fabric_serial,
                server_interface=a.server_interface,
                server_port=a.server_port,
                fabric_port=a.fabric_port,
                admin_state=a.admin_state,
                oper_state=a.oper_state,
                speed_mbps=a.speed_mbps,
                interface_kind=a.interface_kind,
                last_seen=now,
            )
            for a in ps.attachments
        ]
        connectivity = Connectivity(
            attachments=attachments, facts=compute_connectivity_facts(attachments)
        )

        hardware = Hardware(
            cpu=Cpu(
                sockets=_carry_forward(
                    ps.cpu_sockets,
                    existing_hardware.cpu.sockets if existing_hardware else None,
                    default=0,
                    unread=unread,
                    name="hardware.cpu.sockets",
                ),
                cores=_carry_forward(
                    ps.cpu_cores,
                    existing_hardware.cpu.cores if existing_hardware else None,
                    default=0,
                    unread=unread,
                    name="hardware.cpu.cores",
                ),
                threads=_carry_forward(
                    ps.cpu_threads,
                    existing_hardware.cpu.threads if existing_hardware else None,
                    default=0,
                    unread=unread,
                    name="hardware.cpu.threads",
                ),
                model=_carry_forward(
                    ps.cpu_model,
                    existing_hardware.cpu.model if existing_hardware else None,
                    default=None,
                    unread=unread,
                    name="hardware.cpu.model",
                ),
            ),
            memory=Memory(
                total_bytes=_carry_forward(
                    ps.memory_total_bytes,
                    existing_hardware.memory.total_bytes if existing_hardware else None,
                    default=0,
                    unread=unread,
                    name="hardware.memory.total_bytes",
                ),
                modules=_carry_forward(
                    [_memory_module_from_dict(m) for m in ps.memory_modules]
                    if ps.memory_modules is not None
                    else None,
                    existing_hardware.memory.modules if existing_hardware else None,
                    default=[],
                    unread=unread,
                    name="hardware.memory.modules",
                ),
            ),
            storage=Storage(
                total_bytes=_carry_forward(
                    ps.storage_total_bytes,
                    existing_hardware.storage.total_bytes if existing_hardware else None,
                    default=0,
                    unread=unread,
                    name="hardware.storage.total_bytes",
                ),
                drives=_carry_forward(
                    [_drive_from_dict(d) for d in ps.storage_drives]
                    if ps.storage_drives is not None
                    else None,
                    existing_hardware.storage.drives if existing_hardware else None,
                    default=[],
                    unread=unread,
                    name="hardware.storage.drives",
                ),
            ),
            gpus=_carry_forward(
                [_gpu_from_dict(self._gpu_catalog.enrich(g)) for g in ps.gpus]
                if ps.gpus is not None
                else None,
                existing_hardware.gpus if existing_hardware else None,
                default=[],
                unread=unread,
                name="hardware.gpus",
            ),
            power=Power(
                psus=_carry_forward(
                    [_psu_from_dict(p) for p in ps.psus] if ps.psus is not None else None,
                    existing_hardware.power.psus if existing_hardware else None,
                    default=[],
                    unread=unread,
                    name="hardware.power.psus",
                )
            ),
        )

        # The name is the authority; the org path only when it says nothing.
        site_id = parse_site_code(ps.name, self._sites) or parse_site_code(
            ps.profile_dn, self._sites
        )

        # An unreachable run must not bump `last_seen_at`, or a dead server
        # would look freshly seen.
        if ps.reachable:
            last_seen_at = now
            unreachable_since = None
        else:
            last_seen_at = existing.last_seen_at if existing is not None else None
            unreachable_since = (
                existing.unreachable_since
                if existing is not None and existing.unreachable_since is not None
                else now
            )

        server = Server(
            _id=server_id,
            name=ps.name,
            name_normalized=normalize_text(ps.name),
            model=ps.model,
            model_normalized=normalize_text(ps.model),
            identity=identity,
            profile_template=profile_template,
            hardware=hardware,
            network=network,
            connectivity=connectivity,
            site_id=site_id,
            manager_id=ps.manager_id,
            tags=list(ps.tags),
            source_provider=provider_type,
            last_seen_at=last_seen_at,
            reachable=ps.reachable,
            unreachable_since=unreachable_since,
            unread_fields=unread,
            revision=revision,
            created_at=created_at,
            updated_at=now,
            # The carry-forward set — see the module docstring.
            # ponytail: `nics`/`attachments` are two-state (`()`) and cannot
            # join it; docs/architecture.md "Ingestion" has the upgrade path.
            classification=existing.classification if existing is not None else Classification(),
            health=existing.health if existing is not None else Health(),
            maintenance=existing.maintenance if existing is not None else Maintenance(),
            openshift=existing.openshift if existing is not None else OpenShiftLifecycle(),
        )

        if self._classification_service is not None:
            classifiable = ClassifiableServer(
                name=ps.name,
                vendor=vendor,
                manager_type=ManagerType(provider_type),
                site_id=site_id,
                serial=ps.serial,
                model=ps.model,
            )
            result = self._classification_service.classify_with_ruleset(classifiable, ruleset)
            previous_version = existing.classification.classification_version if existing else 0
            server.classification = classification_from_result(
                result, previous_version=previous_version
            )

        if self._health_service is not None:
            state = self._health_service.evaluate_with_policies(server, policies)
            server.health = health_from_state(state)

        server.search_tokens = build_search_tokens(server)
        return server
