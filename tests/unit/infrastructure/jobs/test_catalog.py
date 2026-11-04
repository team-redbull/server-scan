"""The chart's catalog marks each CronJob with its values.yaml flag (ADR-0055)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.ports.job_runner import CronJobInfo
from app.infrastructure.jobs.catalog import apply_catalog, load_catalog

pytestmark = pytest.mark.unit


def _cj(name: str) -> CronJobInfo:
    return CronJobInfo(
        name=name,
        schedule="0 6 * * *",
        suspend=False,
        last_schedule_time=None,
        last_successful_time=None,
        warning=None,
        active=False,
    )


def test_a_catalog_entry_with_no_cronjob_is_listed_as_not_deployed() -> None:
    out = apply_catalog([_cj("r-a")], {"r-a": True, "r-b": False})

    assert [(c.name, c.enabled, c.deployed) for c in out] == [
        ("r-a", True, True),
        ("r-b", False, False),
    ]


def test_a_cronjob_missing_from_the_catalog_counts_as_enabled() -> None:
    (only,) = apply_catalog([_cj("r-a")], {})

    assert only.enabled is True
    assert only.deployed is True


def test_the_catalog_file_is_read_and_a_bad_one_means_no_catalog(tmp_path: Path) -> None:
    good = tmp_path / "catalog.json"
    good.write_text(json.dumps([{"name": "r-a", "enabled": False}]))
    bad = tmp_path / "bad.json"
    bad.write_text("not json")

    assert load_catalog(str(good)) == {"r-a": False}
    assert load_catalog(str(bad)) == {}
    assert load_catalog(str(tmp_path / "missing.json")) == {}
    assert load_catalog(None) == {}
