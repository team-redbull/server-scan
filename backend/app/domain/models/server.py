"""The `servers` collection: one document per physical machine.

`Identity` fields are ordered by correlation strength (see
`app.domain.services.identity`, slice 2) even though the correlation
*algorithm* isn't implemented yet — declaring the field shape now means
identity correlation lands without a schema migration.

`name_normalized`/`serial_normalized`/`model_normalized`/`search_tokens`
are always present with safe defaults (`""` / `[]`), never null: this is
what makes them safe to sort/index on without special-casing missing
values (see `app.domain.services.search`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import Vendor
from app.domain.models.classification import Classification
from app.domain.models.connectivity import Connectivity
from app.domain.models.hardware import Hardware
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.network import NetworkInfo
from app.domain.models.openshift import OpenShiftLifecycle


class Identity(BaseModel):
    """The identity fields a server is correlated on across collector runs."""

    vendor: Vendor
    serial: str | None = None
    serial_normalized: str = ""
    system_uuid: str | None = None
    nic_macs: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)  # manager_id -> external id


class ProfileTemplate(BaseModel):
    """
    The reusable template this server's profile was provisioned from.

    Vendor-neutral: `name` is the vendor's display name, `external_id` its
    opaque reference. Per-vendor sources: docs/architecture.md, "The provider contract".
    """

    name: str | None = None
    external_id: str | None = None


class Server(BaseModel):
    """One document in the `servers` collection — one physical machine's full record."""

    id: str = Field(alias="_id")
    schema_version: int = 1

    name: str
    name_normalized: str = ""
    model: str | None = None
    model_normalized: str = ""

    identity: Identity
    profile_template: ProfileTemplate = Field(default_factory=ProfileTemplate)
    hardware: Hardware = Field(default_factory=Hardware)
    network: NetworkInfo = Field(default_factory=NetworkInfo)
    connectivity: Connectivity = Field(default_factory=Connectivity)
    classification: Classification = Field(default_factory=Classification)
    health: Health = Field(default_factory=Health)
    maintenance: Maintenance = Field(default_factory=Maintenance)
    openshift: OpenShiftLifecycle = Field(default_factory=OpenShiftLifecycle)

    # A plain `str`, not an enum, so a document outlives a site rename (ADR-0018).
    site_id: str | None = None
    manager_id: str | None = None

    tags: list[str] = Field(default_factory=list)
    search_tokens: list[str] = Field(default_factory=list)

    source_provider: str | None = None
    # When the server's own endpoint last answered — not when ingest last ran.
    last_seen_at: datetime | None = None

    reachable: bool = True  # False: identity known, not reached; hardware carries forward
    unreachable_since: datetime | None = None

    # Rebuilt from scratch every ingest, never merged.
    unread_fields: list[str] = Field(default_factory=list)

    revision: int = 1
    created_at: datetime
    updated_at: datetime

    model_config = {"populate_by_name": True}
