"""A broken in-cluster setup disables the Jobs page instead of aborting startup (ADR-0055)."""

from __future__ import annotations

import pytest

from app.infrastructure.jobs.factory import build_job_runner

pytestmark = pytest.mark.unit


def test_a_missing_ca_file_turns_the_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert build_job_runner("kubernetes", "ns") is None
