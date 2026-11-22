"""`INVENTORY_FAKE_MANAGERS`: the fake fleet answers live reads, in development only (ADR-0064)."""

from __future__ import annotations

from typing import Literal, cast

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


def test_a_degraded_ucs_mirror_carries_its_missing_leg() -> None:
    """The seeded fleet has UCS servers whose mirror lost a leg (ADR-0068)."""
    servers = [
        s
        for s in generate_servers(count=1000, seed=42)
        if provider_type_for(s) == ManagerType.UCS_CENTRAL.value and s.storage_controllers
    ]
    degraded = [
        s
        for s in servers
        if s.storage_controllers is not None
        and any(
            v.get("health") == "WARNING"
            for c in s.storage_controllers
            for v in cast("tuple[dict[str, object], ...]", c.get("volumes", ()))
        )
    ]

    assert degraded
    for server in degraded:
        assert server.storage_drives is not None
        assert any(d.get("health_detail") == "missing" for d in server.storage_drives)


@pytest.mark.parametrize("epoch", [1])
def test_an_epoch_never_heals_the_missing_leg_of_a_degraded_mirror(epoch: int) -> None:
    """An epoch's drive mutation leaves the lost leg CRITICAL, as its volume is still WARNING.

    Count 50000 is deliberate: index 8439 of seed 42 is a degraded UCS server the epoch mutates.
    """
    checked = 0
    for server in generate_servers(count=50000, seed=42, epoch=epoch):
        if not server.storage_controllers or provider_type_for(server) != "UCS_CENTRAL":
            continue
        degraded = any(
            v.get("health") == "WARNING"
            for c in server.storage_controllers
            for v in cast("tuple[dict[str, object], ...]", c.get("volumes", ()))
        )
        if not degraded:
            continue
        checked += 1
        legs = [d for d in server.storage_drives or () if str(d["id"]).endswith("/disk-2")]
        assert [(d["health"], d["health_detail"]) for d in legs] == [("CRITICAL", "missing")]
    assert checked
