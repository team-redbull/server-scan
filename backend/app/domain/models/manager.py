"""The `managers` collection: one projection document per configured collector.

Written by `tools.run_collector.manager_for` from settings on every run so the
API can resolve a server's `manager_id`; never the source of a connection
(ADR-0012). Flat on purpose — the fields a manager hierarchy or per-manager
credentials would need were removed 2026-09-13, unread since ADR-0012.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields


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
    endpoint: str | None = None
    enabled: bool = True
    # Reserved for direct BMC actions (power); a secret's name, never its value.
    audit: AuditFields
    # Written by `record_run`, never by `upsert`, so it survives between runs.
    last_run: ManagerRun | None = None

    model_config = {"populate_by_name": True}
