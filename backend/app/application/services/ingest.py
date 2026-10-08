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
the provider reports, and never touches
`maintenance`/`openshift`/`reservation` at all (those belong to their own
engines) — those three are always carried forward verbatim from the
existing document on update, never reset to zero. `reservation` is the one
where forgetting that is not a lost setting but a correctness bug: it is an
install lock, so wiping it mid-install hands the same machine to a second
cluster. `classification` and `health` are the one exception: when
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

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.application.services.audit_service import SYSTEM_INGEST_ACTOR, AuditService
from app.application.services.pipeline import (
    classification_from_result,
    health_change_data,
    health_from_state,
)
from app.domain.enums import LinkState, ManagerType, MediaType, Vendor
from app.domain.models.audit_event import EventType
from app.domain.models.classification import Classification, classification_changed_data
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
from app.domain.models.reservation import Reservation
from app.domain.models.server import Identity, ProfileTemplate, Server
from app.domain.models.site import Site
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.domain.ports.repository import ServerRepository
from app.domain.services.classification import ClassifiableServer
from app.domain.services.health.evaluate import HealthState
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.domain.value_objects.bmc_address import parse_bmc_address
from app.domain.value_objects.gpu_catalog import GpuCatalog
from app.domain.value_objects.mac_address import normalize_mac
from app.domain.value_objects.site import SiteCatalog, parse_site_code
from app.errors import ConflictError, NotFoundError, RevisionConflictError
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

if TYPE_CHECKING:
    from app.application.services.classification_service import ClassificationService
    from app.application.services.health_policy_service import HealthPolicyService

logger = structlog.get_logger(__name__)

_BMC_MATCHED_PROVIDER = ManagerType.REDFISH_STANDALONE.value

# Bounds the read-build-write loop when another writer keeps moving the document (ADR-0044).
_MAX_WRITE_ATTEMPTS = 3

# Per-run stamps `_build_server` writes fresh every run; anything NOT listed that differs forces a
# full write, so a new one costs an optimisation, never an update (ADR-0044, guard test
# `tests/integration/test_ingest_skip_unchanged.py`).
_VOLATILE_TOP_LEVEL = ("revision", "updated_at", "last_seen_at", "listed_at")

IngestOutcome = Literal["created", "updated", "unchanged"]


class _ServerMoved(Exception):
    """The conditional touch of an unchanged server matched nothing: the document moved."""


def _stable_view(server: Server) -> dict[str, Any]:
    """
    Reduce a server to what a collector run can actually change.

    Args:
        server (Server): A built or stored server.

    Returns:
        dict[str, Any]: Its JSON form without the per-run stamps and counters.
    """
    view = server.model_dump(by_alias=True, mode="json")
    for key in _VOLATILE_TOP_LEVEL:
        view.pop(key, None)
    view["health"].pop("evaluated_at", None)
    view["classification"].pop("classified_at", None)
    view["classification"].pop("classification_version", None)
    for attachment in view["connectivity"]["attachments"]:
        attachment.pop("last_seen", None)
    for gpu in view["hardware"]["gpus"]:
        gpu.pop("temperature_celsius", None)
        gpu.pop("power_watts", None)
    for psu in view["hardware"]["power"]["psus"]:
        psu.pop("power_watts", None)
    return view


def _changed_paths(old: object, new: object, path: str = "") -> set[str]:
    """
    List the leaf paths where two stable views differ, with list indexes collapsed to `[]`.

    Args:
        old (object): The stored server's stable view (or a part of it).
        new (object): The fresh server's stable view (or the same part).
        path (str): The path of this part so far.

    Returns:
        set[str]: Paths such as `hardware.storage.drives[].health`; a list of a different length
            is reported as the list itself.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        found: set[str] = set()
        for key in old.keys() | new.keys():
            found |= _changed_paths(old.get(key), new.get(key), f"{path}.{key}" if path else key)
        return found
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        found = set()
        for a, b in zip(old, new, strict=True):
            found |= _changed_paths(a, b, f"{path}[]")
        return found
    return set() if old == new else {path}


def _has_unset_fields(model: BaseModel) -> bool:
    """
    Whether a stored document predates a field, so it must be rewritten even if unchanged.

    A document written by current code carries every field (`model_dump` keeps `None`s), so a field
    that validation had to fill from its default means the document is older than the model.

    Args:
        model (BaseModel): A model validated from a stored document, or one of its parts.

    Returns:
        bool: True if this model or any nested model has a field missing from the stored form.
    """
    if model.model_fields_set != set(type(model).model_fields):
        return True
    for name in model.model_fields_set:
        value = getattr(model, name)
        children = value if isinstance(value, list) else [value]
        if any(isinstance(c, BaseModel) and _has_unset_fields(c) for c in children):
            return True
    return False


def _seen_fields(
    server: Server, *, runs_health: bool, runs_classification: bool
) -> dict[str, object]:
    """
    The "last confirmed" stamps to refresh on an unchanged server.

    Args:
        server (Server): The freshly built server (its stamps are this run's).
        runs_health (bool): Whether the health engine ran, so its stamp is real.
        runs_classification (bool): Whether the classification engine ran, so its stamp is real.

    Returns:
        dict[str, object]: Dotted path -> new value for `ServerRepository.touch_seen`.
    """
    fields: dict[str, object] = {"listed_at": server.listed_at}
    if server.last_seen_at is not None:
        fields["last_seen_at"] = server.last_seen_at
    if runs_health and server.health.evaluated_at is not None:
        fields["health.evaluated_at"] = server.health.evaluated_at
    if runs_classification and server.classification.classified_at is not None:
        fields["classification.classified_at"] = server.classification.classified_at
    if server.connectivity.attachments:
        fields["connectivity.attachments.$[].last_seen"] = server.connectivity.attachments[
            0
        ].last_seen
    # Equal stable views mean the same GPUs and PSUs in the same order, so index paths are safe.
    for i, gpu in enumerate(server.hardware.gpus):
        fields[f"hardware.gpus.{i}.temperature_celsius"] = gpu.temperature_celsius
        fields[f"hardware.gpus.{i}.power_watts"] = gpu.power_watts
    for i, psu in enumerate(server.hardware.power.psus):
        fields[f"hardware.power.psus.{i}.power_watts"] = psu.power_watts
    return fields


def _as_stored(server: Server, existing: Server) -> Server:
    """
    Make a skipped server's in-memory copy match what is in Mongo.

    Args:
        server (Server): The freshly built (but not written) server.
        existing (Server): The stored server it equals in content.

    Returns:
        Server: `server` with the stored `revision`, `updated_at` and classification counter.
    """
    server.revision = existing.revision
    server.updated_at = existing.updated_at
    server.classification = server.classification.model_copy(
        update={"classification_version": existing.classification.classification_version}
    )
    return server


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
    unchanged: int = 0
    errors: int = 0
    changed_paths: Counter[str] = field(default_factory=Counter)


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
        power_watts=_opt_float(data.get("power_watts")),
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
                _server, outcome = await self._ingest_one(
                    provider_server,
                    provider_type=provider.provider_type,
                    ruleset=ruleset,
                    policies=policies,
                    changed_paths=summary.changed_paths,
                )
            except Exception:
                logger.exception(
                    "ingest.server_failed",
                    external_id=provider_server.external_id,
                    vendor=provider_server.vendor,
                )
                summary.errors += 1
                continue

            if outcome == "created":
                summary.created += 1
            elif outcome == "unchanged":
                summary.unchanged += 1
            else:
                summary.updated += 1

        logger.info(
            "ingest.completed",
            fetched=summary.fetched,
            created=summary.created,
            updated=summary.updated,
            unchanged=summary.unchanged,
            errors=summary.errors,
            top_changed_paths=summary.changed_paths.most_common(10),
        )
        return summary

    async def ingest_one(self, ps: ProviderServer, *, provider_type: str) -> Server:
        """
        Run one already-fetched provider record through the full ingest pipeline.

        For a live single-server recheck; see ADR-0032.

        Args:
            ps (ProviderServer): The freshly fetched record for one server.
            provider_type (str): The collector's `ManagerType` value.

        Returns:
            Server: The persisted, re-evaluated server.
        """
        ruleset = (
            await self._classification_service.load_ruleset()
            if self._classification_service is not None
            else []
        )
        policies = (
            await self._health_service.load_policies() if self._health_service is not None else []
        )
        server, _outcome = await self._ingest_one(
            ps, provider_type=provider_type, ruleset=ruleset, policies=policies
        )
        return server

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

    async def _find_by_bmc_host(self, ps: ProviderServer, *, has_serial: bool) -> Server | None:
        """
        Match a standalone-Redfish record to a document by BMC host (ADR-0037 decision 5).

        A serial-less record updates its host's document; a serial-bearing one
        adopts only a serial-less stub, never another machine's document.

        Args:
            ps (ProviderServer): The provider's record.
            has_serial (bool): Whether `ps` carries a serial with no document yet.

        Returns:
            Server | None: The document to update in place, or `None` to create one.
        """
        parsed = parse_bmc_address(ps.bmc_address_raw)
        if parsed is None or not parsed.host:
            return None
        matches = await self._server_repo.find_by_bmc_host(_BMC_MATCHED_PROVIDER, parsed.host)
        if has_serial:
            matches = [m for m in matches if not m.identity.serial_normalized]
        if not matches:
            return None
        matches.sort(key=lambda m: not m.identity.serial_normalized)
        if len(matches) > 1:
            logger.warning(
                "ingest.bmc_host_ambiguous",
                host=parsed.host,
                server_ids=[m.id for m in matches],
            )
        return matches[0]

    async def _ingest_one(
        self,
        ps: ProviderServer,
        *,
        provider_type: str,
        ruleset: list[ClassificationRule],
        policies: list[HealthPolicy],
        changed_paths: Counter[str] | None = None,
    ) -> tuple[Server, IngestOutcome]:
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
            changed_paths (Counter[str] | None): Where to tally which fields made an existing
                server count as changed (`_changed_paths`), for the `ingest.completed` log.

        Returns:
            tuple[Server, IngestOutcome]: The persisted server, and whether it was
                `created`, `updated` (content changed, full write) or `unchanged` (only
                the "last confirmed" stamps were refreshed, ADR-0044).
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

        for attempt in range(1, _MAX_WRITE_ATTEMPTS + 1):
            existing = (
                await self._find_by_vendor_serial(vendor, serial_normalized)
                if serial_normalized
                else None
            )
            if existing is None and provider_type == _BMC_MATCHED_PROVIDER:
                existing = await self._find_by_bmc_host(ps, has_serial=bool(serial_normalized))
            doc_vendor = (
                existing.identity.vendor
                if existing is not None and not serial_normalized
                else vendor
            )

            server, state = await self._build_server(
                ps,
                vendor=doc_vendor,
                serial_normalized=serial_normalized,
                existing=existing,
                provider_type=provider_type,
                ruleset=ruleset,
                policies=policies,
            )

            new_view = _stable_view(server)
            old_view = _stable_view(existing) if existing is not None else None
            unchanged = (
                existing is not None and not _has_unset_fields(existing) and new_view == old_view
            )
            if old_view is not None and changed_paths is not None and not unchanged:
                changed_paths.update(_changed_paths(old_view, new_view))
            try:
                if existing is None:
                    await self._server_repo.upsert(server)
                elif unchanged:
                    touched = await self._server_repo.touch_seen(
                        existing.id,
                        expected_revision=existing.revision,
                        fields=_seen_fields(
                            server,
                            runs_health=self._health_service is not None,
                            runs_classification=self._classification_service is not None,
                        ),
                    )
                    if not touched:
                        raise _ServerMoved
                else:
                    await self._server_repo.upsert_with_revision_check(
                        server, expected_revision=existing.revision
                    )
            except (DuplicateKeyError, RevisionConflictError, NotFoundError, _ServerMoved) as exc:
                # The document moved under us (docs/adr/0044): a concurrent insert on
                # `uniq_vendor_serial`, or maintenance/reservation/membership/prune writing
                # between our read and our write. Re-read and rebuild; never overwrite it.
                logger.info(
                    "ingest.write_conflict",
                    external_id=ps.external_id,
                    attempt=attempt,
                    reason=type(exc).__name__,
                )
                if attempt < _MAX_WRITE_ATTEMPTS:
                    continue
                if isinstance(exc, DuplicateKeyError):
                    raise
                # A 404/409 from the repository would be misleading to a caller of
                # `GET /servers/available`; say what actually happened.
                raise ConflictError(
                    f"Server {ps.name!r} kept changing while it was being ingested; try again."
                ) from exc

            if existing is not None and unchanged:
                return _as_stored(server, existing), "unchanged"
            await self._emit_transition_events(existing, server, state, policies)
            return server, "created" if existing is None else "updated"

        raise AssertionError("unreachable: the last failed attempt re-raises")  # pragma: no cover

    async def _emit_transition_events(
        self,
        existing: Server | None,
        server: Server,
        state: HealthState | None,
        policies: list[HealthPolicy],
    ) -> None:
        """
        Audit only the ingestion transitions worth an entry.

        A new server, or an engine verdict that changed — never a generic
        update, which every run would emit for every server.

        Args:
            existing (Server | None): The server as it was before this
                upsert, or `None` if it was just created.
            server (Server): The server as it now stands, already upserted.
            state (HealthState | None): The health evaluation behind `server`,
                kept alongside it so the event can say why; `None` if no
                health engine ran.
            policies (list[HealthPolicy]): This run's policy snapshot.
        """
        if self._audit is None:
            return

        if existing is None:
            await self._audit.record(
                EventType.SERVER_CREATED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                server_name=server.name,
                data={"vendor": server.identity.vendor.value, "name": server.name},
            )
            return

        if server.classification.installation_type != existing.classification.installation_type:
            await self._audit.record(
                EventType.CLASSIFICATION_CHANGED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                server_name=server.name,
                data=classification_changed_data(
                    existing.classification.installation_type, server.classification
                ),
            )

        if state is not None and server.health.overall != existing.health.overall:
            await self._audit.record(
                EventType.HEALTH_CHANGED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                server_name=server.name,
                data=health_change_data(existing.health, state, policies),
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
    ) -> tuple[Server, HealthState | None]:
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
            tuple[Server, HealthState | None]: The built document, classified
                and health-evaluated when the engines were supplied, and the
                health evaluation (for the audit event's reasons).
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

        if not serial_normalized and existing is not None:
            serial, serial_normalized = (
                existing.identity.serial,
                existing.identity.serial_normalized,
            )
            system_uuid = ps.system_uuid or existing.identity.system_uuid
        else:
            serial, system_uuid = ps.serial, ps.system_uuid
        identity = Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=serial_normalized,
            system_uuid=system_uuid,
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

        # A stub read nothing, so it must not bump `last_seen_at`; a server the
        # manager did read (OneView with a dead iLO) still advances it.
        hardware_read = ps.cpu_sockets is not None or ps.memory_total_bytes is not None
        if ps.reachable:
            last_seen_at = now
            unreachable_since = None
            unreachable_reason = None
        else:
            unreachable_reason = ps.unreachable_reason
            last_seen_at = (
                now if hardware_read else (existing.last_seen_at if existing is not None else None)
            )
            unreachable_since = (
                existing.unreachable_since
                if existing is not None and existing.unreachable_since is not None
                else now
            )

        model = ps.model
        if model is None and not ps.reachable and existing is not None:
            model = existing.model
        server = Server(
            _id=server_id,
            name=ps.name,
            name_normalized=normalize_text(ps.name),
            model=model,
            model_normalized=normalize_text(model),
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
            unreachable_reason=unreachable_reason,
            listed_at=now,
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
            # Carried forward for the same reason maintenance is, and with
            # more at stake: a wiped reservation does not lose a setting, it
            # hands a machine that is mid-install to a second cluster.
            reservation=existing.reservation if existing is not None else Reservation(),
            openshift=existing.openshift if existing is not None else OpenShiftLifecycle(),
        )

        if self._classification_service is not None:
            classifiable = ClassifiableServer(
                name=ps.name,
                vendor=vendor,
                manager_type=ManagerType(provider_type),
                site_id=site_id,
                serial=serial,
                model=model,
            )
            result = self._classification_service.classify_with_ruleset(classifiable, ruleset)
            previous_version = existing.classification.classification_version if existing else 0
            server.classification = classification_from_result(
                result, previous_version=previous_version
            )

        state: HealthState | None = None
        if self._health_service is not None:
            state = self._health_service.evaluate_with_policies(server, policies)
            server.health = health_from_state(state)

        server.search_tokens = build_search_tokens(server)
        return server, state
