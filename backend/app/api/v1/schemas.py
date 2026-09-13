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

from collections import Counter
from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import Vendor
from app.domain.models.classification import Classification
from app.domain.models.connectivity import Connectivity, ConnectivityFacts
from app.domain.models.hardware import Hardware
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.network import NetworkInfo
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, ProfileTemplate, Server
from app.domain.value_objects.nic_names import NicNameCatalog, cisco_eno_names
from app.infrastructure.mongodb.server_repository import FacetRow


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
    reachable: bool
    unreachable_since: datetime | None
    updated_at: datetime

    @classmethod
    def from_server(cls, server: Server) -> ServerSummary:
        """
        Build the list-response projection from a domain server.

        Args:
            server (Server): The stored document.

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
    reachable: bool
    unreachable_since: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_server(cls, server: Server, nic_names: NicNameCatalog = _NO_NIC_NAMES) -> ServerDetail:
        """
        Build the detail response for one server.

        Args:
            server (Server): The stored document.
            nic_names (NicNameCatalog): The configured FQDD-to-OS-name
                mapping. Defaults to an empty one, which renders the
                hardware names alone rather than inventing any.

        Returns:
            ServerDetail: The response model.
        """
        nic_os_names = {
            interface.name: os_name
            for interface in server.network.interfaces
            if (os_name := nic_names.os_name_for(interface.name)) is not None
        }
        if server.identity.vendor == Vendor.CISCO:
            # Positional, not configured — see `cisco_eno_names`.
            nic_os_names.update(
                cisco_eno_names([interface.name for interface in server.network.interfaces])
            )
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
            reachable=server.reachable,
            unreachable_since=server.unreachable_since,
            revision=server.revision,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )


class AvailableServerItem(BaseModel):
    """One `GET /servers/available` result: a full server plus this call's own metadata."""

    server: ServerDetail
    live_recheck_performed: bool


class AvailableServersResponse(BaseModel):
    """`GET /servers/available`'s envelope — always a list (ADR-0032)."""

    items: list[AvailableServerItem]
    mode: str
    requested: int
    returned: int


class ServerFacets(BaseModel):
    """
    How many servers each filter option would match, for one view.

    Counts are within the filters already applied, and an option with no
    match is absent, not zero — docs/architecture.md, "Search, pagination".

    Attributes:
        total (int): Servers matching the current filters.
        vendor (dict[str, int]): Counts by `identity.vendor`.
        source_provider (dict[str, int]): Counts by collector.
        installation_type (dict[str, int]): Counts by classification.
        health_overall (dict[str, int]): Counts by health severity.
        maintenance (dict[str, int]): Counts keyed `"true"`/`"false"`,
            strings because JSON object keys cannot be booleans.
    """

    total: int
    vendor: dict[str, int] = Field(default_factory=dict)
    source_provider: dict[str, int] = Field(default_factory=dict)
    installation_type: dict[str, int] = Field(default_factory=dict)
    health_overall: dict[str, int] = Field(default_factory=dict)
    maintenance: dict[str, int] = Field(default_factory=dict)
    openshift_state: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Iterable[FacetRow]) -> ServerFacets:
        """
        Sum the grouped rows into one marginal per dimension.

        Args:
            rows (Iterable[FacetRow]): `MongoServerRepository.
                facet_breakdown`'s output.

        Returns:
            ServerFacets: The per-option counts.
        """
        totals: dict[str, Counter[str]] = {
            "vendor": Counter(),
            "source_provider": Counter(),
            "installation_type": Counter(),
            "health_overall": Counter(),
            "maintenance": Counter(),
            "openshift_state": Counter(),
        }
        total = 0
        for row in rows:
            total += row.count
            for dimension, value in (
                ("vendor", row.vendor),
                ("source_provider", row.source_provider),
                ("installation_type", row.installation_type),
                ("health_overall", row.health_overall),
                ("maintenance", "true" if row.maintenance else "false"),
                ("openshift_state", row.openshift_state),
            ):
                # `None` counts in `total` but under no option: no filter
                # value would select it.
                if value is not None:
                    totals[dimension][str(value)] += row.count
        return cls(
            total=total,
            vendor=dict(totals["vendor"]),
            source_provider=dict(totals["source_provider"]),
            installation_type=dict(totals["installation_type"]),
            health_overall=dict(totals["health_overall"]),
            maintenance=dict(totals["maintenance"]),
            openshift_state=dict(totals["openshift_state"]),
        )
