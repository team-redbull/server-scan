"""Every login outcome is exported from startup, so a rare one shows up on the dashboard."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from app.observability.metrics import LOGIN_OUTCOMES

pytestmark = pytest.mark.unit

_ROUTE = Path(__file__).resolve().parents[3] / "backend" / "app" / "api" / "v1" / "auth.py"


@pytest.mark.parametrize("outcome", LOGIN_OUTCOMES)
def test_each_outcome_exists_before_any_login(outcome: str) -> None:
    assert REGISTRY.get_sample_value("auth_logins_total", {"outcome": outcome}) is not None


def test_the_login_route_only_uses_outcomes_the_metric_knows() -> None:
    used = set(re.findall(r'outcome = "([a-z_]+)"', _ROUTE.read_text()))
    assert used, "the login route no longer assigns an outcome: update this guard"
    assert used <= set(LOGIN_OUTCOMES)
