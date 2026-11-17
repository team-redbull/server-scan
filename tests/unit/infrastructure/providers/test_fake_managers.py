"""`INVENTORY_FAKE_MANAGERS`: the fake fleet answers live reads, in development only (ADR-0062)."""

from __future__ import annotations

from typing import Literal

import pytest

from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.ports.provider import ServerIdentity
from app.infrastructure.providers.factory import (
    build_provider_for_manager_type,
    fake_manager_provider,
)
from app.infrastructure.providers.fake.generator import generate_servers, provider_type_for
from app.infrastructure.providers.fake.provider import FakeProvider

pytestmark = pytest.mark.unit

Environment = Literal["development", "test", "staging", "production"]


def _settings(spec: str, environment: Environment = "development") -> Settings:
    return Settings(
        _env_file=None,
        fake_managers=spec,
        environment=environment,
        cursor_secret="unit-test-cursor-secret",
    )


async def test_development_reads_the_seeded_fleet() -> None:
    record = next(
        s
        for s in generate_servers(seed=42, count=60)
        if provider_type_for(s) == ManagerType.ONEVIEW.value
    )

    provider = build_provider_for_manager_type(ManagerType.ONEVIEW, settings=_settings("42:60"))

    assert isinstance(provider, FakeProvider)
    found = await provider.get_one(ServerIdentity(serial=record.serial, name=record.name))
    assert found is not None
    assert found.external_id == record.external_id


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
def test_every_other_environment_ignores_it(environment: Environment) -> None:
    assert fake_manager_provider(ManagerType.ONEVIEW, _settings("42:60", environment)) is None


@pytest.mark.parametrize("spec", ["", "42", "forty:two", "42:60:x"])
def test_unset_or_malformed_means_the_real_manager(spec: str) -> None:
    assert fake_manager_provider(ManagerType.ONEVIEW, _settings(spec)) is None
