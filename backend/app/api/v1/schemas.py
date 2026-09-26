"""API response schemas for `/api/v1/servers`.

Neither of these is the persistence model (`app.domain.models.server.
Server`) reused as-is — an explicit project requirement, not just a style
preference:

* `ServerSummary` is a deliberately lighter projection for list responses.
  At 10k+ servers, shipping the full `hardware` subdocument (CPU, every
  memory module, every drive, every PSU) on every row of a list response
  is pure waste — nothing in a list view renders it. `connectivity.facts`
  is the one exception kept in the summary: it's small (four scalars) and
  drives a fabric-health column in the list UI, so leaving it out would
  just cause the frontend to issue a detail fetch per row.
* `ServerDetail` is a field-for-field superset of `Server` today, but it's
  still its own model rather than `Server` returned directly, for two
  concrete reasons: (1) `Server.id` is aliased to `_id` for MongoDB, and
  FastAPI's default `response_model_by_alias=True` would serialize that
  alias straight into the API response, leaking a Mongo-ism into the
  public contract; (2) returning the persistence model directly means any
  storage-only field added to `Server` in a later slice is silently
  exposed over the API with no seam to stop and ask "should this be
  public?" — a dedicated response schema is that seam, even when today it
  happens to mirror every field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter

from app.domain.enums import (
    HealthSeverity,
    InstallationType,
    LinkState,
    ManagerType,
    OpenShiftState,
    Vendor,
)
from app.domain.models.classification import Classification
from app.domain.models.connectivity import Connectivity, ConnectivityFacts
from app.domain.models.hardware import Hardware
from app.domain.models.health import Health, decode_retired_severity
from app.domain.models.maintenance import Maintenance
from app.domain.models.network import BmcInfo, NetworkInfo
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, ProfileTemplate, Server
from app.domain.value_objects.nic_names import NicNameCatalog, cisco_eno_names


class ConnectivitySummary(BaseModel):
    """`ServerSummary`'s slice of `Connectivity`.

    Facts only, never the full `attachments` list (that's detail-only; see
    the module docstring on why summaries stay lean).
    """

    facts: ConnectivityFacts


class ServerSummary(BaseModel):
    """List-response projection of a server.

    Nested to match `ServerDetail`'s shape — one nesting convention across
    both endpoints, not two.
    """

    id: str
    name: str
    vendor: Vendor
    model: str | None
    site_id: str | None
    manager_id: str | None
    source_provider: str | None
    classification: Classification
    health: Health
    maintenance: Maintenance
    # Whole sub-model: a row also shows the cluster holding it.
    openshift: OpenShiftLifecycle
    connectivity: ConnectivitySummary
    last_seen_at: datetime | None
    stale: bool
    reachable: bool
    unreachable_since: datetime | None
    updated_at: datetime

    @classmethod
    def from_server(cls, server: Server, *, stale_before: datetime) -> ServerSummary:
        """
        Build the list-response projection from a domain server.

        Args:
            server (Server): The stored document.
            stale_before (datetime): A server last seen before this — or
                never — is `stale` (ADR-0029's window, on the API's clock).

        Returns:
            ServerSummary: The response model.
        """
        return cls(
            id=server.id,
            name=server.name,
            vendor=server.identity.vendor,
            model=server.model,
            site_id=server.site_id,
            manager_id=server.manager_id,
            source_provider=server.source_provider,
            classification=server.classification,
            health=server.health,
            maintenance=server.maintenance,
            openshift=server.openshift,
            connectivity=ConnectivitySummary(facts=server.connectivity.facts),
            last_seen_at=server.last_seen_at,
            stale=is_stale(server, stale_before),
            reachable=server.reachable,
            unreachable_since=server.unreachable_since,
            updated_at=server.updated_at,
        )


class PageInfo(BaseModel):
    """Keyset paging metadata for a page of servers."""

    next_cursor: str | None
    has_more: bool
    page_size: int
    count: int | None
    count_capped: bool


class ServerListResponse(BaseModel):
    """One page of servers plus its paging metadata."""

    items: list[ServerSummary]
    page: PageInfo


_NO_NIC_NAMES = NicNameCatalog(names_by_kind={})


def is_stale(server: Server, stale_before: datetime) -> bool:
    """
    Whether a server's own endpoint has not answered since the cutoff.

    The same rule the fleet gauges use (ADR-0029): never seen counts as stale.

    Args:
        server (Server): The stored document.
        stale_before (datetime): The cutoff, `now - INVENTORY_STALE_AFTER_SECONDS`.

    Returns:
        bool: `True` when `last_seen_at` is absent or older than the cutoff.
    """
    return server.last_seen_at is None or server.last_seen_at < stale_before


def _nic_os_names(server: Server, nic_names: NicNameCatalog) -> dict[str, str]:
    """
    The OS-level name for each of a server's interfaces, where one is known.

    Args:
        server (Server): The stored document.
        nic_names (NicNameCatalog): The configured FQDD-to-OS-name mapping.

    Returns:
        dict[str, str]: `NetworkInterface.name` -> OS name. Dell from the
            catalog, Cisco positionally (`cisco_eno_names`); an interface
            with no known name is absent rather than guessed.
    """
    names = {
        interface.name: os_name
        for interface in server.network.interfaces
        if (os_name := nic_names.os_name_for(interface.name)) is not None
    }
    if server.identity.vendor == Vendor.CISCO:
        names.update(cisco_eno_names([interface.name for interface in server.network.interfaces]))
    return names


_OPTIONAL_DATETIME = TypeAdapter(datetime | None)


class MaintenanceFlag(BaseModel):
    """The two maintenance fields an inventory row shows."""

    enabled: bool
    reason: str | None


class ServerRow(BaseModel):
    """
    One flat inventory row for `GET /servers/rows` (ADR-0033).

    Only what the inventory table renders or searches on; the detail page
    fetches the rest. Built from a Mongo projection, never from `Server`.
    """

    id: str
    name: str
    vendor: Vendor
    model: str | None
    site_id: str | None
    source_provider: str | None
    installation_type: InstallationType
    health: HealthSeverity
    maintenance: MaintenanceFlag
    openshift_state: OpenShiftState
    cluster_name: str | None
    mce_name: str | None
    profile_template_name: str | None
    last_seen_at: datetime | None
    stale: bool
    reachable: bool
    serial: str | None
    bmc_host: str | None
    macs: list[str]

    @classmethod
    def from_doc(cls, doc: dict[str, Any], *, stale_before: datetime) -> ServerRow:
        """
        Build a row from a projected server document.

        Args:
            doc (dict[str, Any]): A document from `MongoServerRepository.list_rows`.
            stale_before (datetime): The staleness cutoff, as for `is_stale`.

        Returns:
            ServerRow: The row.
        """
        seen = _OPTIONAL_DATETIME.validate_python(doc.get("last_seen_at"))
        network = doc.get("network") or {}
        return cls(
            id=doc["_id"],
            name=doc["name"],
            vendor=doc["identity"]["vendor"],
            model=doc.get("model"),
            site_id=doc.get("site_id"),
            source_provider=doc.get("source_provider"),
            installation_type=(doc.get("classification") or {}).get(
                "installation_type", InstallationType.UNCLASSIFIED
            ),
            health=decode_retired_severity(
                (doc.get("health") or {}).get("overall", HealthSeverity.UNKNOWN)
            ),
            maintenance=MaintenanceFlag(
                enabled=(doc.get("maintenance") or {}).get("enabled", False),
                reason=(doc.get("maintenance") or {}).get("reason"),
            ),
            openshift_state=(doc.get("openshift") or {}).get(
                "lifecycle_state", OpenShiftState.AVAILABLE
            ),
            cluster_name=(doc.get("openshift") or {}).get("cluster_name"),
            mce_name=(doc.get("openshift") or {}).get("mce_name"),
            profile_template_name=(doc.get("profile_template") or {}).get("name"),
            last_seen_at=seen,
            stale=seen is None or seen < stale_before,
            reachable=doc.get("reachable", True),
            serial=doc["identity"].get("serial"),
            bmc_host=(network.get("bmc") or {}).get("host"),
            macs=[mac for iface in network.get("interfaces") or [] if (mac := iface.get("mac"))],
        )


class ServerRowsResponse(BaseModel):
    """The whole fleet as inventory rows, plus when the inventory last changed."""

    items: list[ServerRow]
    generated_at: datetime | None

    @classmethod
    def from_docs(cls, docs: list[dict[str, Any]], *, stale_before: datetime) -> ServerRowsResponse:
        """
        Build the response.

        `generated_at` is the newest `updated_at`, so an unchanged fleet
        yields byte-identical bodies and therefore one ETag.

        Args:
            docs (list[dict[str, Any]]): Projected documents from `list_rows`.
            stale_before (datetime): The staleness cutoff.

        Returns:
            ServerRowsResponse: The response.
        """
        stamps: list[str] = [doc["updated_at"] for doc in docs if doc.get("updated_at")]
        newest = max(stamps, default=None)
        return cls(
            items=[ServerRow.from_doc(doc, stale_before=stale_before) for doc in docs],
            generated_at=_OPTIONAL_DATETIME.validate_python(newest),
        )


class ServerDetail(BaseModel):
    """Full server detail.

    See the module docstring for why this is a dedicated model rather than
    `Server` returned as-is.
    """

    id: str
    schema_version: int
    name: str
    name_normalized: str
    model: str | None
    model_normalized: str
    identity: Identity
    profile_template: ProfileTemplate
    hardware: Hardware
    network: NetworkInfo
    connectivity: Connectivity
    classification: Classification
    health: Health
    maintenance: Maintenance
    openshift: OpenShiftLifecycle
    site_id: str | None
    manager_id: str | None
    tags: list[str]
    search_tokens: list[str]
    source_provider: str | None
    unread_fields: list[str]
    # Derived from `INVENTORY_NIC_OS_NAMES`, not collected — hence beside
    # `network`, not inside it.
    nic_os_names: dict[str, str] = Field(default_factory=dict)
    last_seen_at: datetime | None
    stale: bool
    reachable: bool
    unreachable_since: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_server(
        cls,
        server: Server,
        nic_names: NicNameCatalog = _NO_NIC_NAMES,
        *,
        stale_before: datetime,
    ) -> ServerDetail:
        """
        Build the detail response for one server.

        Args:
            server (Server): The stored document.
            nic_names (NicNameCatalog): The configured FQDD-to-OS-name
                mapping. Defaults to an empty one, which renders the
                hardware names alone rather than inventing any.
            stale_before (datetime): A server last seen before this — or
                never — is `stale`.

        Returns:
            ServerDetail: The response model.
        """
        nic_os_names = _nic_os_names(server, nic_names)
        return cls(
            id=server.id,
            schema_version=server.schema_version,
            name=server.name,
            name_normalized=server.name_normalized,
            model=server.model,
            model_normalized=server.model_normalized,
            identity=server.identity,
            profile_template=server.profile_template,
            hardware=server.hardware,
            network=server.network,
            connectivity=server.connectivity,
            classification=server.classification,
            health=server.health,
            maintenance=server.maintenance,
            openshift=server.openshift,
            site_id=server.site_id,
            manager_id=server.manager_id,
            tags=server.tags,
            search_tokens=server.search_tokens,
            source_provider=server.source_provider,
            unread_fields=server.unread_fields,
            nic_os_names=nic_os_names,
            last_seen_at=server.last_seen_at,
            stale=is_stale(server, stale_before),
            reachable=server.reachable,
            unreachable_since=server.unreachable_since,
            revision=server.revision,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )


class AvailableInterface(BaseModel):
    """One NIC as a BMH/NMState generator needs it: name, MAC, placement, link state.

    `link_state` is what lets a caller bond only up ports — ADR-0032's
    2026-09-25 update, which also records how unevenly vendors report it.
    """

    name: str
    mac: str | None
    location: str | None
    os_name: str | None
    link_state: LinkState
    speed_mbps: int | None


def bmc_vendor_for(vendor: Vendor, source_provider: str | None) -> str | None:
    """
    The BMC-driver vocabulary a BMH generator keys its `bmc.address` scheme on.

    `INTERSIGHT` is a fourth value beside `HP`/`DELL`/`CISCO` — ADR-0032's 2026-09-13 update.

    Args:
        vendor (Vendor): `identity.vendor`.
        source_provider (str | None): The collector's `ManagerType` value.

    Returns:
        str | None: `HP`, `DELL`, `CISCO` or `INTERSIGHT`; `None` for a
            `STANDALONE` machine, whose driver the caller must decide.
    """
    if vendor == Vendor.CISCO:
        return "INTERSIGHT" if source_provider == ManagerType.INTERSIGHT.value else "CISCO"
    if vendor == Vendor.DELL:
        return "DELL"
    if vendor == Vendor.HP:
        return "HP"
    return None


class AvailableServerItem(BaseModel):
    """
    One `GET /servers/available` result — only what a BMH/NMState generator consumes.

    Deliberately not `ServerDetail` (ADR-0032, 2026-09-13 update): the
    caller builds a `BareMetalHost` and an `NMStateConfig`, nothing else.
    """

    id: str
    name: str
    vendor: Vendor
    source_provider: str | None
    bmc_vendor: str | None
    bmc: BmcInfo
    nic_macs: list[str]
    interfaces: list[AvailableInterface]
    site_id: str | None
    health_overall: HealthSeverity
    live_recheck_performed: bool

    @classmethod
    def from_server(
        cls, server: Server, *, nic_names: NicNameCatalog, live_recheck_performed: bool
    ) -> AvailableServerItem:
        """
        Project one (freshly rechecked) server onto the generator-facing shape.

        Args:
            server (Server): The server as persisted by the live recheck, or
                as stored when the recheck was skipped.
            nic_names (NicNameCatalog): The configured FQDD-to-OS-name mapping.
            live_recheck_performed (bool): Whether `get_one()` actually ran.

        Returns:
            AvailableServerItem: The response item.
        """
        os_names = _nic_os_names(server, nic_names)
        return cls(
            id=server.id,
            name=server.name,
            vendor=server.identity.vendor,
            source_provider=server.source_provider,
            bmc_vendor=bmc_vendor_for(server.identity.vendor, server.source_provider),
            bmc=server.network.bmc,
            nic_macs=list(server.identity.nic_macs),
            interfaces=[
                AvailableInterface(
                    name=interface.name,
                    mac=interface.mac,
                    location=interface.location,
                    os_name=os_names.get(interface.name),
                    link_state=interface.link_state,
                    speed_mbps=interface.speed_mbps,
                )
                for interface in server.network.interfaces
            ],
            site_id=server.site_id,
            health_overall=server.health.overall,
            live_recheck_performed=live_recheck_performed,
        )


class AvailableServersResponse(BaseModel):
    """`GET /servers/available`'s envelope — always a list (ADR-0032)."""

    items: list[AvailableServerItem]
    mode: str
    requested: int
    returned: int
