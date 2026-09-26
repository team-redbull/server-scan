"""`AvailableServersService` against real Mongo — health-tier ranking, the
random draw, the live-recheck-downgrades-a-candidate replacement path, and
capacity-token aliasing. See docs/adr/0032-available-server-lookup-api.md.

`_no_provider` stands in for every manager type having no credentials on
this process (Decision 5's degrade path): candidates are ranked from Mongo
and returned trusting the stored document, unchanged. The one test that
needs an actual live recheck uses the real `FakeProvider` with
`get_one_overrides`, exactly as ADR-0032 says the fake provider must
support to make a downgrade-and-replace scenario observable at all.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.application.services.available_servers import (
    AvailableServersNotFoundError,
    AvailableServersService,
)
from app.application.services.ingest import IngestService
from app.domain.enums import HealthSeverity, ManagerType, OpenShiftState, Vendor
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.domain.value_objects.capacity_aliases import capacity_alias_catalog
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.provider import FakeProvider
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"
_MANAGER_ID = "mgr_test_available"
SITES = site_catalog("")
CAPACITY_ALIASES = capacity_alias_catalog("")


def _ingest_service(mongo: MongoClientHolder) -> IngestService:
    return IngestService(
        sites=SITES,
        server_repo=MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
    )


def _no_provider(_manager_type: ManagerType) -> ServerInventoryProvider:
    """Every manager type unconfigured — the degrade-to-Mongo path (ADR-0032, Decision 5)."""
    raise ManagerNotConfiguredError("not configured for this test")


def _server(
    index: int,
    *,
    name: str | None = None,
    health: HealthSeverity = HealthSeverity.HEALTHY,
    # The draw requires the network category to have been READ and passed, so
    # a fixture leaving it UNKNOWN is excluded — ADR-0032, 2026-09-26 update.
    network_health: HealthSeverity = HealthSeverity.HEALTHY,
    reachable: bool = True,
    maintenance: bool = False,
    lifecycle_state: OpenShiftState = OpenShiftState.AVAILABLE,
    external_id: str | None = None,
) -> Server:
    now = utcnow()
    nm = name if name is not None else f"ocp-avail-test-{index:04d}"
    serial = f"AVAIL{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=nm,
        name_normalized=normalize_text(nm),
        identity=Identity(
            vendor=Vendor.STANDALONE,
            serial=serial,
            serial_normalized=normalize_text(serial),
            external_ids=({_MANAGER_ID: external_id} if external_id else {}),
        ),
        manager_id=_MANAGER_ID,
        source_provider=ManagerType.REDFISH_STANDALONE.value,
        health=Health(overall=health, network=network_health),
        maintenance=Maintenance(enabled=maintenance),
        openshift=OpenShiftLifecycle(lifecycle_state=lifecycle_state),
        reachable=reachable,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


async def _seed(mongo: MongoClientHolder, *servers: Server) -> MongoServerRepository:
    repo = MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET)
    for server in servers:
        await repo.upsert(server)
    return repo


def _service(
    mongo: MongoClientHolder,
    *,
    provider_factory: Callable[[ManagerType], ServerInventoryProvider] = _no_provider,
) -> AvailableServersService:
    return AvailableServersService(
        repo=MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        ingest=_ingest_service(mongo),
        provider_factory=provider_factory,
        capacity_aliases=CAPACITY_ALIASES,
    )


class TestPatternModeRanking:
    async def test_fills_from_the_best_tier_first(self, mongo_holder: MongoClientHolder) -> None:
        await _seed(
            mongo_holder,
            _server(0, name="ocp-tier-01", health=HealthSeverity.HEALTHY),
            _server(1, name="ocp-tier-02", health=HealthSeverity.WARNING),
        )
        outcome = await _service(mongo_holder).lookup_by_pattern(
            "ocp-tier", count=1, extra_filters={}
        )

        assert outcome.requested == 1
        assert len(outcome.items) == 1
        assert outcome.items[0].server.name == "ocp-tier-01"
        assert outcome.items[0].live_recheck_performed is False

    async def test_never_selects_critical_or_unknown(self, mongo_holder: MongoClientHolder) -> None:
        await _seed(
            mongo_holder,
            _server(0, name="ocp-bad-01", health=HealthSeverity.CRITICAL),
            _server(1, name="ocp-bad-02", health=HealthSeverity.UNKNOWN),
        )

        with pytest.raises(AvailableServersNotFoundError):
            await _service(mongo_holder).lookup_by_pattern("ocp-bad", count=1, extra_filters={})

    async def test_no_name_matches_the_pattern_at_all(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        with pytest.raises(AvailableServersNotFoundError) as excinfo:
            await _service(mongo_holder).lookup_by_pattern(
                "no-such-server-exists", count=1, extra_filters={}
            )
        assert "no server name matches" in excinfo.value.reason

    async def test_partial_fulfillment_is_not_an_error(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(
            mongo_holder,
            _server(0, name="ocp-partial-01"),
            _server(1, name="ocp-partial-02"),
        )

        outcome = await _service(mongo_holder).lookup_by_pattern(
            "ocp-partial", count=5, extra_filters={}
        )

        assert outcome.requested == 5
        assert len(outcome.items) == 2

    async def test_maintenance_and_unavailable_servers_are_excluded(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(
            mongo_holder,
            _server(0, name="ocp-excl-maint", maintenance=True),
            _server(1, name="ocp-excl-installed", lifecycle_state=OpenShiftState.INSTALLED),
            _server(2, name="ocp-excl-unreachable", reachable=False),
        )

        with pytest.raises(AvailableServersNotFoundError):
            await _service(mongo_holder).lookup_by_pattern("ocp-excl", count=1, extra_filters={})

    async def test_the_random_draw_varies_across_repeated_calls(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, *(_server(i, name=f"ocp-rand-{i:02d}") for i in range(10)))
        service = _service(mongo_holder)

        draws = [
            frozenset(
                item.server.id
                for item in (
                    await service.lookup_by_pattern("ocp-rand", count=3, extra_filters={})
                ).items
            )
            for _ in range(8)
        ]

        assert len(set(draws)) > 1


class TestCapacityAliasExpansion:
    async def test_5tb_also_matches_bare_hypershift_not_hypershift_data(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(
            mongo_holder,
            _server(0, name="ocp-hypershift-01"),
            _server(1, name="ocp-hypershift-data-01"),
        )

        outcome = await _service(mongo_holder).lookup_by_pattern("5tb", count=5, extra_filters={})

        names = {item.server.name for item in outcome.items}
        assert names == {"ocp-hypershift-01"}

    async def test_10tb_matches_hypershift_data(self, mongo_holder: MongoClientHolder) -> None:
        await _seed(
            mongo_holder,
            _server(0, name="ocp-hypershift-02"),
            _server(1, name="ocp-hypershift-data-02"),
        )

        outcome = await _service(mongo_holder).lookup_by_pattern("10tb", count=5, extra_filters={})

        names = {item.server.name for item in outcome.items}
        assert names == {"ocp-hypershift-data-02"}

    async def test_a_compound_pattern_containing_the_token_is_not_expanded(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, _server(0, name="ocp-dell-r650-five-128c-1024gb-5tb-DEL0001"))

        outcome = await _service(mongo_holder).lookup_by_pattern(
            "ocp-dell-r650-five-128c-1024gb-5tb-", count=1, extra_filters={}
        )

        assert len(outcome.items) == 1
        assert outcome.items[0].server.name == "ocp-dell-r650-five-128c-1024gb-5tb-DEL0001"


class TestLiveRecheckReplacement:
    async def test_a_downgraded_candidate_is_replaced_from_the_next_tier(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        """The one HEALTHY candidate is found unreachable on recheck and excluded;
        the WARNING-tier candidate is drawn as its replacement.
        """
        downgraded = _server(
            0, name="ocp-replace-01", health=HealthSeverity.HEALTHY, external_id="ext-downgrade"
        )
        replacement = _server(
            1,
            name="ocp-replace-02",
            health=HealthSeverity.WARNING,
            external_id="ext-replacement",
        )
        await _seed(mongo_holder, downgraded, replacement)

        provider = FakeProvider(
            seed=1,
            count=1,
            provider_type=ManagerType.REDFISH_STANDALONE.value,
            get_one_overrides={
                "ext-downgrade": ProviderServer(
                    external_id="ext-downgrade",
                    vendor=Vendor.STANDALONE.value,
                    name=downgraded.name,
                    serial=downgraded.identity.serial,
                    reachable=False,
                ),
                "ext-replacement": ProviderServer(
                    external_id="ext-replacement",
                    vendor=Vendor.STANDALONE.value,
                    name=replacement.name,
                    serial=replacement.identity.serial,
                    reachable=True,
                ),
            },
        )
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        outcome = await service.lookup_by_pattern("ocp-replace", count=1, extra_filters={})

        assert len(outcome.items) == 1
        assert outcome.items[0].server.name == "ocp-replace-02"
        assert outcome.items[0].live_recheck_performed is True


class TestNameMode:
    async def test_exact_match_is_case_insensitive(self, mongo_holder: MongoClientHolder) -> None:
        await _seed(mongo_holder, _server(0, name="OCP-Case-Exact-01"))

        result = await _service(mongo_holder).lookup_by_name("ocp-case-exact-01", extra_filters={})

        assert result.server.name == "OCP-Case-Exact-01"

    async def test_no_server_has_that_name(self, mongo_holder: MongoClientHolder) -> None:
        with pytest.raises(AvailableServersNotFoundError):
            await _service(mongo_holder).lookup_by_name("no-such-name", extra_filters={})

    async def test_no_longer_available_after_recheck_raises(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        server = _server(0, name="ocp-name-downgrade", external_id="ext-name-downgrade")
        await _seed(mongo_holder, server)

        provider = FakeProvider(
            seed=1,
            count=1,
            provider_type=ManagerType.REDFISH_STANDALONE.value,
            get_one_overrides={
                "ext-name-downgrade": ProviderServer(
                    external_id="ext-name-downgrade",
                    vendor=Vendor.STANDALONE.value,
                    name=server.name,
                    serial=server.identity.serial,
                    reachable=False,
                )
            },
        )
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        with pytest.raises(AvailableServersNotFoundError) as excinfo:
            await service.lookup_by_name("ocp-name-downgrade", extra_filters={})
        assert "no longer available" in excinfo.value.reason
