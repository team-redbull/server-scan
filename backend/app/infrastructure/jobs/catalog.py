"""The chart's list of every triggerable job and whether `values.yaml` enables it (ADR-0055)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import structlog

from app.domain.ports.job_runner import CronJobInfo

logger = structlog.get_logger(__name__)


def load_catalog(path: str | None) -> dict[str, bool]:
    """
    Read the mounted catalog; a missing or malformed file means "no catalog".

    Args:
        path (str | None): `INVENTORY_JOBS_CATALOG_FILE`, a JSON list of `{name, enabled}`.

    Returns:
        dict[str, bool]: CronJob name to its `values.yaml` flag; empty when unavailable.
    """
    if not path:
        return {}
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
        return {str(i["name"]): bool(i["enabled"]) for i in items}
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("jobs.catalog_unreadable", path=path)
        return {}


def apply_catalog(cronjobs: list[CronJobInfo], catalog: dict[str, bool]) -> list[CronJobInfo]:
    """
    Mark each CronJob with its `values.yaml` flag and add the ones the cluster does not have.

    Args:
        cronjobs (list[CronJobInfo]): What the runner found in the cluster.
        catalog (dict[str, bool]): CronJob name to its flag.

    Returns:
        list[CronJobInfo]: Sorted by name; a catalog entry with no CronJob is `deployed=False`.
    """
    live = {
        c.name: replace(c, enabled=catalog[c.name]) if c.name in catalog else c for c in cronjobs
    }
    for name, enabled in catalog.items():
        if name not in live:
            live[name] = CronJobInfo(
                name=name,
                schedule="",
                suspend=False,
                last_schedule_time=None,
                last_successful_time=None,
                warning=None,
                active=False,
                enabled=enabled,
                deployed=False,
            )
    return sorted(live.values(), key=lambda c: c.name)
