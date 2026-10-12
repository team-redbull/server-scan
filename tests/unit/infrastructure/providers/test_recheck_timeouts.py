"""`recheck_timeout_seconds`: each manager's own timeout for a live `/servers/available` recheck."""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.infrastructure.providers.factory import recheck_timeout_seconds

pytestmark = pytest.mark.unit


def _settings(spec: str) -> Settings:
    return Settings(_env_file=None, available_recheck_timeouts=spec)


def test_each_manager_uses_its_own_configured_timeout() -> None:
    settings = _settings("UCS_CENTRAL:240,INTERSIGHT:10,ONEVIEW:30")

    assert recheck_timeout_seconds(ManagerType.UCS_CENTRAL, settings) == 240
    assert recheck_timeout_seconds(ManagerType.INTERSIGHT, settings) == 10
    assert recheck_timeout_seconds(ManagerType.ONEVIEW, settings) == 30


def test_an_unlisted_manager_uses_the_shared_collector_default() -> None:
    settings = _settings("UCS_CENTRAL:240")

    assert recheck_timeout_seconds(ManagerType.OPENMANAGE, settings) == 15.0


def test_blank_means_the_default_for_everyone() -> None:
    assert recheck_timeout_seconds(ManagerType.UCS_CENTRAL, _settings("")) == 15.0


@pytest.mark.parametrize("spec", ["UCS_CENTRAL", "UCS_CENTRAL:abc", "UCS_CENTRAL:0", "NOPE:5"])
def test_a_malformed_entry_is_skipped_not_fatal(spec: str) -> None:
    assert recheck_timeout_seconds(ManagerType.UCS_CENTRAL, _settings(spec)) == 15.0


def test_names_are_case_and_space_tolerant() -> None:
    settings = _settings(" ucs_central : 240 , oneview:20")

    assert recheck_timeout_seconds(ManagerType.UCS_CENTRAL, settings) == 240
    assert recheck_timeout_seconds(ManagerType.ONEVIEW, settings) == 20
