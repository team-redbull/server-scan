"""The `managers` collection: one projection document per configured collector.

Written by `tools.run_collector.manager_for` from settings on every run so the
API can resolve a server's `manager_id`; never the source of a connection
(ADR-0012). Flat on purpose — the fields a manager hierarchy or per-manager
credentials would need were removed 2026-09-13, unread since ADR-0012.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields

_NOT_SLUG = re.compile(r"[^a-z0-9-]+")

# The one KUBEVIRT manager id before each cluster got its own (ADR-0060); a KUBEVIRT run deletes it.
LEGACY_KUBEVIRT_MANAGER_ID = "mgr_kubevirt"


class ManagerRun(BaseModel):
    """What the collector's most recent run reported — see ADR-0029."""

    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    servers_fetched: int
    servers_created: int
    servers_updated: int
    # Of `servers_updated`, how many were unchanged and not rewritten (ADR-0044); 0 on a run
    # recorded before that existed.
    servers_unchanged: int = 0
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


def kubevirt_cluster_slug(cluster_name: str) -> str:
    """
    A KubeVirt cluster name as it appears in that cluster's manager id.

    Args:
        cluster_name (str): `INVENTORY_KUBEVIRT_CLUSTER_NAME`, as configured.

    Returns:
        str: Lowercased, every run outside `[a-z0-9-]` made one `-`, edge dashes
            stripped; `""` when nothing usable is left.
    """
    return _NOT_SLUG.sub("-", cluster_name.strip().lower()).strip("-")


def manager_id_and_name(
    manager_type: ManagerType, *, kubevirt_cluster: str = ""
) -> tuple[str, str]:
    """
    The deterministic `Manager` id and name one collector writes.

    One per type, except `KUBEVIRT`: each cluster runs its own collector, so its
    cluster is part of both (`mgr_kubevirt_<slug>`, `kubevirt-<slug>`).

    Args:
        manager_type (ManagerType): The collector's type.
        kubevirt_cluster (str): The KubeVirt cluster name; ignored for every other type.

    Returns:
        tuple[str, str]: The manager's id and name.

    Raises:
        ValueError: `KUBEVIRT` with a cluster name that slugs to nothing.
    """
    base = manager_type.value.lower()
    if manager_type is not ManagerType.KUBEVIRT:
        return f"mgr_{base}", base.replace("_", "-")
    slug = kubevirt_cluster_slug(kubevirt_cluster)
    if not slug:
        raise ValueError(f"a KUBEVIRT manager needs a cluster name, got {kubevirt_cluster!r}")
    return f"mgr_{base}_{slug}", f"{base}-{slug}"
