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

# UCS Manager is the only manager type that currently has a real parent
# hierarchy (UCS Central owns UCS Manager domains). Declared explicitly so
# an invalid hierarchy (e.g. an OPENMANAGE manager under a UCS_CENTRAL
# parent) is a validation error, not a silently accepted document.
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
    # Exit 3: the run did not see the whole fleet for a reason worth a
    # human looking at (tools.run_collector's PARTIAL decision).
    partial: bool


class Manager(BaseModel):
    """One document in the `managers` collection — a projection of a collector's configuration."""

    id: str = Field(alias="_id")
    name: str
    type: ManagerType
    site_id: str | None = None
    parent_manager_id: str | None = None
    endpoint: str | None = None
    enabled: bool = True
    # Reserved and unused: talking to a BMC directly (redfish/IPMI, for
    # power actions) is a separate concern from querying a manager, with
    # a different blast radius, and will need its own credentials when
    # that lands. Kept as a name rather than a value — no plaintext
    # secret ever belongs in a document.
    bmc_credential_ref: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    audit: AuditFields
    # Run state, not configuration: written by `record_run`, never by the
    # config projection `upsert`, so it survives between runs.
    last_run: ManagerRun | None = None

    model_config = {"populate_by_name": True}
