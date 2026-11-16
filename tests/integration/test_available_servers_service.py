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
from datetime import timedelta

import pytest
from structlog.testing import capture_logs
from tests.uncached_logger import UncachedLogger

import app.application.services.available_servers as available_servers_module
from app.application.services.audit_service import AuditService
from app.application.services.available_servers import (
    SELECTABLE_TIERS,
    AvailableServersNotFoundError,
    AvailableServersService,
)
from app.application.services.ingest import IngestService
from app.application.services.reservation_service import ReservationClaim, ReservationService
from app.domain.enums import HealthSeverity, ManagerType, OpenShiftState, Vendor
from app.domain.models.audit_event import Actor, ActorType
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.ports.provider import (
    NotThisManagersServerError,
    ProviderServer,
    ServerInventoryProvider,
)
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.domain.value_objects.capacity_aliases import capacity_alias_catalog
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
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


class DegradingIngest(IngestService):
    """An ingest whose live check ends with a chosen health per server name, as a policy run would.

    The test ingest has no health policy service, so this stands in for one: it persists the
    degraded health the way `ingest_one` persists a real evaluation.
    """

    def __init__(self, mongo: MongoClientHolder, healths: dict[str, HealthSeverity]) -> None:
        """
        Build over the same repositories as `_ingest_service`.

        Args:
            mongo (MongoClientHolder): The test database.
            healths (dict[str, HealthSeverity]): Health to end with, by server name.
        """
        repo = MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET)
        super().__init__(
            sites=SITES,
            server_repo=repo,
            site_repo=MongoSiteRepository(mongo),
            manager_repo=MongoManagerRepository(mongo),
        )
        self._repo = repo
        self._healths = healths

    async def ingest_one(self, ps: ProviderServer, *, provider_type: str) -> Server:
        """
        Ingest, then overwrite the health this server's name maps to.

        Args:
            ps (ProviderServer): The live record.
            provider_type (str): The collector's manager type.

        Returns:
            Server: The persisted server, with the chosen health when one is mapped.
        """
        server = await super().ingest_one(ps, provider_type=provider_type)
        health = self._healths.get(server.name)
        if health is None:
            return server
        expected = server.revision
        server.health = server.health.model_copy(update={"overall": health})
        server.revision += 1
        await self._repo.upsert_with_revision_check(server, expected_revision=expected)
        return server


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
    max_attempts: int = 5,
    ingest: IngestService | None = None,
) -> AvailableServersService:
    repo = MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET)
    return AvailableServersService(
        repo=repo,
        ingest=ingest or _ingest_service(mongo),
        provider_factory=provider_factory,
        capacity_aliases=CAPACITY_ALIASES,
        reservations=ReservationService(
            server_repo=repo, audit=AuditService(repo=MongoAuditEventRepository(mongo))
        ),
        max_attempts=max_attempts,
    )


ACTOR = Actor(type=ActorType.USER, id="tester")


def _claim(workflow_id: str = "wf-1", ttl_seconds: int = 3600) -> ReservationClaim:
    return ReservationClaim(
        holder="install-server",
        mce_cluster="mce-a",
        infra_env=None,
        namespace=None,
        workflow_id=workflow_id,
        ttl=timedelta(seconds=ttl_seconds),
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
        """The one HEALTHY candidate is found unreachable on the live check and released;
        the WARNING-tier candidate is locked and returned instead.
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

        result = await service.reserve_by_pattern(
            "ocp-replace",
            claim=_claim(),
            actor=ACTOR,
            request_id=None,
            extra_filters={},
            tiers=SELECTABLE_TIERS,
        )

        assert result.server.name == "ocp-replace-02"
        assert result.live_recheck_performed is True


class _ExplodingProvider(FakeProvider):
    async def get_one(self, identity: object) -> ProviderServer | None:
        raise RuntimeError("vendor manager returned garbage")


class TestRecheckLogging:
    async def test_a_failing_live_check_is_logged_with_its_manager_and_fails_the_reservation(
        self, mongo_holder: MongoClientHolder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(available_servers_module, "logger", UncachedLogger())
        server = _server(0, name="ocp-log-boom", external_id="ext-log-boom")
        await _seed(mongo_holder, server)
        provider = _ExplodingProvider(
            seed=1, count=1, provider_type=ManagerType.REDFISH_STANDALONE.value
        )
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        with capture_logs() as logs, pytest.raises(AvailableServersNotFoundError):
            await service.reserve_by_name(
                "ocp-log-boom",
                claim=_claim(),
                actor=ACTOR,
                request_id=None,
                extra_filters={},
                tiers=SELECTABLE_TIERS,
            )

        events = {line["event"]: line for line in logs}
        assert events["available.recheck_failed"]["manager_type"] == "REDFISH_STANDALONE"
        assert events["available.recheck"]["outcome"] == "failed"


class _ForeignProvider(FakeProvider):
    async def get_one(self, identity: object) -> ProviderServer | None:
        raise NotThisManagersServerError("another cluster's facade")


class TestAnotherManagersServer:
    """Another KubeVirt cluster's VM is trusted from Mongo, never dropped (`skipped_foreign`)."""

    async def test_is_returned_unrechecked_and_logged(
        self, mongo_holder: MongoClientHolder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(available_servers_module, "logger", UncachedLogger())
        await _seed(mongo_holder, _server(0, name="ocp-foreign-vm", external_id="ext-foreign"))
        provider = _ForeignProvider(seed=1, count=1, provider_type=ManagerType.KUBEVIRT.value)
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        with capture_logs() as logs:
            result = await service.reserve_by_name(
                "ocp-foreign-vm",
                claim=_claim(),
                actor=ACTOR,
                request_id=None,
                extra_filters={},
                tiers=SELECTABLE_TIERS,
            )

        assert result.server.name == "ocp-foreign-vm"
        assert result.live_recheck_performed is False
        events = {line["event"]: line for line in logs}
        assert events["available.recheck"]["outcome"] == "skipped_foreign"

    async def test_pattern_mode_keeps_it_as_a_candidate(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, _server(0, name="ocp-fvm-1", external_id="ext-f1"))
        provider = _ForeignProvider(seed=1, count=1, provider_type=ManagerType.KUBEVIRT.value)
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        result = await service.reserve_by_pattern(
            "ocp-fvm-",
            claim=_claim(),
            actor=ACTOR,
            request_id=None,
            extra_filters={},
            tiers=SELECTABLE_TIERS,
        )

        assert result.server.name == "ocp-fvm-1"
        assert result.live_recheck_performed is False


class TestVendorFailure:
    """A vendor error fails that candidate (released, next tried); all failing is a not-found."""

    async def test_the_failure_names_the_server_and_the_cause_and_frees_the_lock(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, _server(0, name="ocp-vendor-down", external_id="ext-vd"))
        provider = _ExplodingProvider(seed=1, count=1, provider_type=ManagerType.UCS_CENTRAL.value)
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        with pytest.raises(AvailableServersNotFoundError) as raised:
            await service.reserve_by_name(
                "ocp-vendor-down",
                claim=_claim(),
                actor=ACTOR,
                request_id=None,
                extra_filters={},
                tiers=SELECTABLE_TIERS,
            )

        assert raised.value.codes == ("VENDOR_ERROR",)
        assert (
            "ocp-vendor-down: RuntimeError: vendor manager returned garbage" in raised.value.reason
        )
        repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
        server = await repo.find_one_by_name("ocp-vendor-down", filters={})
        assert server is not None
        assert server.reservation.holder is None

    async def test_every_candidate_failing_is_one_not_found_naming_each(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(
            mongo_holder,
            _server(0, name="ocp-pat-1", external_id="ext-p1"),
            _server(1, name="ocp-pat-2", external_id="ext-p2"),
        )
        provider = _ExplodingProvider(seed=1, count=2, provider_type=ManagerType.UCS_CENTRAL.value)
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        with pytest.raises(AvailableServersNotFoundError) as raised:
            await service.reserve_by_pattern(
                "ocp-pat-",
                claim=_claim(),
                actor=ACTOR,
                request_id=None,
                extra_filters={},
                tiers=SELECTABLE_TIERS,
            )

        assert "ocp-pat-1" in raised.value.reason
        assert "ocp-pat-2" in raised.value.reason
        assert "2 live-checked" in raised.value.reason


class TestNameMode:
    async def test_exact_match_is_case_insensitive(self, mongo_holder: MongoClientHolder) -> None:
        await _seed(mongo_holder, _server(0, name="OCP-Case-Exact-01"))

        result = await _service(mongo_holder).lookup_by_name("ocp-case-exact-01", extra_filters={})

        assert result.server.name == "OCP-Case-Exact-01"

    async def test_no_server_has_that_name(self, mongo_holder: MongoClientHolder) -> None:
        with pytest.raises(AvailableServersNotFoundError):
            await _service(mongo_holder).lookup_by_name("no-such-name", extra_filters={})

    async def test_no_longer_available_after_the_live_check_fails_the_reservation(
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
            await service.reserve_by_name(
                "ocp-name-downgrade",
                claim=_claim(),
                actor=ACTOR,
                request_id=None,
                extra_filters={},
                tiers=SELECTABLE_TIERS,
            )
        assert "its BMC is unreachable" in excinfo.value.reason
        assert excinfo.value.codes == ("UNREACHABLE",)
