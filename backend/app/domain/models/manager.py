"""The `managers` collection.

`parent_manager_id` models the UCS Central -> UCS Manager hierarchy
observed in the user's existing UCS operator (`BareMetalHostUCS`): it logs
into UCS Central first, reads a service profile's `.domain`, then opens a
second session to that specific UCS Manager domain. Every other manager
type is currently flat (`parent_manager_id=None`), so the field costs
nothing for Dell/HPE/Intersight and models the one real hierarchy Cisco
UCS actually has.

This document is a *projection of configuration*, not its source: a
collector derives it from the environment (`tools.run_collector.
manager_for`) and upserts it so the API and UI can resolve a server's
`manager_id` to something readable. Where a manager is and how to log
into it live in settings, one endpoint and login per manager type — see
`app.domain.ports.credentials`. There is deliberately no `credential_ref`
here any more: a reference to a secret is only useful when several
managers of one type need different credentials, which this platform's
one-per-type model does not have.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields

# The one real hierarchy: UCS Central owns UCS Manager domains. Nothing
# validates against this yet.
ALLOWED_PARENT_TYPES: dict[ManagerType, frozenset[ManagerType]] = {
    ManagerType.UCS_MANAGER: frozenset({ManagerType.UCS_CENTRAL}),
}


class ManagerRun(BaseModel):
    """What the collector's most recent run reported — see ADR-0029."""

    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    servers_fetched: int
    servers_created: int
    servers_updated: int
    ingest_errors: int
    collection_errors: int
    partial: bool  # exit 3: tools.run_collector's PARTIAL decision


class Manager(BaseModel):
    """One document in the `managers` collection — a projection of a collector's configuration."""

    id: str = Field(alias="_id")
    name: str
    type: ManagerType
    site_id: str | None = None
    parent_manager_id: str | None = None
    endpoint: str | None = None
    enabled: bool = True
    # Reserved for direct BMC actions (power); a secret's name, never its value.
    bmc_credential_ref: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    audit: AuditFields
    # Written by `record_run`, never by `upsert`, so it survives between runs.
    last_run: ManagerRun | None = None

    model_config = {"populate_by_name": True}
