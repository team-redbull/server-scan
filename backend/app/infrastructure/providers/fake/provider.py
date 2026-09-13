"""`ServerInventoryProvider` implementation backed by deterministic fake data.

Exercises the ingestion pipeline against the same seam every real collector
implements. One instance stands in for one collector, via `provider_type`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import SiteCatalog
from app.infrastructure.providers.fake.generator import (
    COLLECTOR_TYPES,
    generate_servers,
    provider_type_for,
)


class FakeProvider(ServerInventoryProvider):
    """`ServerInventoryProvider` for deterministic fake data.

    `seed`, `count` and `provider_type` are fixed at construction, so
    `collect()` yields the same servers every call on a given instance.
    """

    def __init__(
        self,
        *,
        seed: int,
        count: int,
        provider_type: str,
        sites: SiteCatalog | None = None,
        get_one_overrides: dict[str, ProviderServer] | None = None,
    ) -> None:
        """Store the parameters that determine which fake servers this instance yields.

        Args:
            seed (int): The generator seed.
            count (int): How many servers the whole fake fleet holds — not
                how many this provider yields, which is the subset this
                collector would have found.
            provider_type (str): The collector this instance imitates, a
                `ManagerType` value.
            sites (SiteCatalog | None): The sites whose codes appear in
                generated hostnames, or None for the shipped default.
            get_one_overrides (dict[str, ProviderServer] | None): Test-only
                seam, keyed by `external_id` — `get_one` returns the
                override instead of regenerating the fleet, so a test can
                simulate a live recheck that downgrades a candidate
                (docs/adr/0032-available-server-lookup-api.md). Never set
                in production wiring.
        """
        super().__init__()
        self._seed = seed
        self._count = count
        self.provider_type = provider_type
        self._sites = sites
        self._get_one_overrides = get_one_overrides or {}

    async def health_check(self) -> None:
        """No real backend to check — the fake provider is always healthy."""
        return

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """Yield this collector's share of the fake fleet.

        Yields:
            ProviderServer: Each fake server this collector would own.
        """
        for server in generate_servers(seed=self._seed, count=self._count, sites=self._sites):
            if provider_type_for(server) == self.provider_type:
                yield server

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """Find one already-generated server by identity, regenerating the deterministic fleet.

        Args:
            identity (ServerIdentity): `external_id`, `serial` or `name`,
                tried in that order.

        Returns:
            ProviderServer | None: The matching server, an injected
                override, or `None` if none of the identity fields match.
        """
        if identity.external_id in self._get_one_overrides:
            return self._get_one_overrides[identity.external_id]
        for server in generate_servers(seed=self._seed, count=self._count, sites=self._sites):
            if provider_type_for(server) != self.provider_type:
                continue
            if identity.external_id and server.external_id == identity.external_id:
                return server
            if identity.serial and server.serial == identity.serial:
                return server
            if identity.name and server.name == identity.name:
                return server
        return None


def fake_providers(
    *, seed: int, count: int, sites: SiteCatalog | None = None
) -> list[FakeProvider]:
    """
    One provider per collector the platform actually runs.

    Together they yield the whole fake fleet exactly once, each server
    behind the collector that would really have found it.

    Args:
        seed (int): The generator seed.
        count (int): How many servers the fleet holds in total.
        sites (SiteCatalog | None): The sites whose codes appear in
            generated hostnames, or None for the shipped default.

    Returns:
        list[FakeProvider]: A provider per implemented collector.
    """
    return [
        FakeProvider(seed=seed, count=count, provider_type=manager_type.value, sites=sites)
        for manager_type in COLLECTOR_TYPES
    ]
