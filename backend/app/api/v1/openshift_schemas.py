"""Response models for `GET/POST /api/v1/openshift/clusters` (docs/adr/0067)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ClusterJobItem(BaseModel):
    """One membership job: a cluster's `nodes` job or an MCE's `agents` job."""

    kind: str
    name: str
    held: int
    last_run_at: datetime
    silent_seconds: int
    silent: bool


class ClusterJobsResponse(BaseModel):
    """Every job with a run record, longest silent first."""

    items: list[ClusterJobItem]
    silent_after_seconds: int


class DecommissionResponse(BaseModel):
    """What a decommission did."""

    kind: str
    name: str
    freed: int
