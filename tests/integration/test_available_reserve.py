"""`AvailableServersService.reserve_by_*` against real Mongo: lock, check, release, cap, races.

The behaviour under test is docs/adr/0063-available-reserves-and-verifies.md. A live check is
a `FakeProvider` with `get_one_overrides`, as in `test_available_servers_service.py`.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.test_available_servers_service import (
    _MANAGER_ID,
    ACTOR,
    DegradingIngest,
    _claim,
    _seed,
    _server,
    _service,
)

from app.application.services.available_servers import (
    AvailableServerResult,
    AvailableServersNotFoundError,
    AvailableServersService,
    tiers_up_to,
    unreserved_filters,
)
from app.domain.enums import HealthSeverity, ManagerType, Vendor
from app.domain.models.audit_event import EventType
from app.domain.ports.provider import ProviderServer
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.providers.fake.provider import FakeProvider
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_SECRET = "test-cursor-secret"


def _repo(mongo: MongoClientHolder) -> MongoServerRepository:
    return MongoServerRepository(mongo, cursor_secret=_SECRET)


def _live(server_name: str, serial: str, ext: str) -> ProviderServer:
    return ProviderServer(
        external_id=ext,
        vendor=Vendor.STANDALONE.value,
        name=server_name,
        serial=serial,
        reachable=True,
        manager_id=_MANAGER_ID,
    )


def _provider(overrides: dict[str, ProviderServer]) -> FakeProvider:
    return FakeProvider(
        seed=1,
        count=1,
        provider_type=ManagerType.REDFISH_STANDALONE.value,
        get_one_overrides=overrides,
    )


async def _reserve(
    service: AvailableServersService,
    pattern: str,
    *,
    workflow_id: str = "wf-1",
    tiers: tuple[HealthSeverity, ...] = (
        HealthSeverity.HEALTHY,
        HealthSeverity.WARNING,
        HealthSeverity.MAJOR,
    ),
) -> AvailableServerResult:
    return await service.reserve_by_pattern(
        pattern,
        claim=_claim(workflow_id),
        actor=ACTOR,
        request_id=None,
        extra_filters={},
        tiers=tiers,
    )


class TestTiersUpTo:
    @pytest.mark.parametrize(
        ("max_health", "expected"),
        [
            (HealthSeverity.HEALTHY, ["HEALTHY"]),
            (HealthSeverity.WARNING, ["HEALTHY", "WARNING"]),
            (HealthSeverity.MAJOR, ["HEALTHY", "WARNING", "MAJOR"]),
            (HealthSeverity.CRITICAL, ["HEALTHY", "WARNING", "MAJOR", "CRITICAL"]),
            (HealthSeverity.UNKNOWN, []),
        ],
    )
    def test_each_max_health_allows_that_tier_and_every_better_one(
        self, max_health: HealthSeverity, expected: list[str]
    ) -> None:
        assert [tier.value for tier in tiers_up_to(max_health)] == expected


class TestReserveKeepsTheLock:
    async def test_the_returned_server_is_locked_for_the_full_ttl_and_audited(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, _server(0, name="ocp-lock-01"))

        result = await _reserve(_service(mongo_holder), "ocp-lock")

        reservation = result.server.reservation
        assert reservation.holder == "install-server"
        assert reservation.mce_cluster == "mce-a"
        assert reservation.workflow_id == "wf-1"
        assert reservation.expires_at is not None
        remaining = (reservation.expires_at - utcnow()).total_seconds()
        assert 3500 < remaining <= 3600
        stored = await _repo(mongo_holder).get_by_id(result.server.id)
        assert stored is not None
        assert stored.reservation.expires_at == reservation.expires_at
        events = await MongoAuditEventRepository(mongo_holder).list_page(
            event_type=EventType.SERVER_RESERVED.value
        )
        assert [e.server_name for e in events.items] == ["ocp-lock-01"]
        assert events.items[0].data["via"] == "available"

    async def test_a_locked_server_is_not_drawn_again(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, _server(0, name="ocp-once-01"))
        service = _service(mongo_holder)

        await _reserve(service, "ocp-once")
        with pytest.raises(AvailableServersNotFoundError) as raised:
            await _reserve(service, "ocp-once", workflow_id="wf-2")

        assert "RESERVED" in raised.value.codes


class TestRejectedCandidatesAreReleased:
    async def test_a_healthy_server_that_reads_critical_is_released_and_the_next_returned(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        bad = _server(0, name="ocp-rel-01", health=HealthSeverity.HEALTHY, external_id="ext-bad")
        good = _server(1, name="ocp-rel-02", health=HealthSeverity.WARNING, external_id="ext-good")
        await _seed(mongo_holder, bad, good)
        provider = _provider(
            {
                "ext-bad": _live(bad.name, bad.identity.serial or "", "ext-bad"),
                "ext-good": _live(good.name, good.identity.serial or "", "ext-good"),
            }
        )
        service = _service(
            mongo_holder,
            provider_factory=lambda _mt: provider,
            ingest=DegradingIngest(mongo_holder, {"ocp-rel-01": HealthSeverity.CRITICAL}),
        )

        result = await _reserve(service, "ocp-rel")

        assert result.server.name == "ocp-rel-02"
        stored_bad = await _repo(mongo_holder).get_by_id(bad.id)
        assert stored_bad is not None
        assert stored_bad.reservation.holder is None
        events = await MongoAuditEventRepository(mongo_holder).list_page(
            event_type=EventType.SERVER_RESERVED.value
        )
        assert [e.server_name for e in events.items] == ["ocp-rel-02"]

    async def test_the_attempt_cap_stops_after_that_many_live_checks(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        servers = [
            _server(i, name=f"ocp-cap-{i:02d}", external_id=f"ext-cap-{i}") for i in range(8)
        ]
        await _seed(mongo_holder, *servers)
        provider = _provider(
            {
                f"ext-cap-{i}": ProviderServer(
                    external_id=f"ext-cap-{i}",
                    vendor=Vendor.STANDALONE.value,
                    name=s.name,
                    serial=s.identity.serial,
                    reachable=False,
                )
                for i, s in enumerate(servers)
            }
        )
        service = _service(mongo_holder, provider_factory=lambda _mt: provider, max_attempts=5)

        with pytest.raises(AvailableServersNotFoundError) as raised:
            await _reserve(service, "ocp-cap")

        assert "5 live-checked" in raised.value.reason
        repo = _repo(mongo_holder)
        held = [
            stored.name
            for s in servers
            if (stored := await repo.get_by_id(s.id)) is not None
            and stored.reservation.holder is not None
        ]
        assert held == []


class TestMaxHealth:
    async def test_a_server_that_degrades_past_max_health_is_rejected(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        server = _server(0, name="ocp-mh-01", external_id="ext-mh")
        await _seed(mongo_holder, server)
        provider = _provider({"ext-mh": _live(server.name, server.identity.serial or "", "ext-mh")})
        service = _service(
            mongo_holder,
            provider_factory=lambda _mt: provider,
            ingest=DegradingIngest(mongo_holder, {"ocp-mh-01": HealthSeverity.MAJOR}),
        )

        with pytest.raises(AvailableServersNotFoundError) as raised:
            await _reserve(service, "ocp-mh", tiers=tiers_up_to(HealthSeverity.WARNING))
        assert "HEALTH" in raised.value.codes

        accepted = await _reserve(service, "ocp-mh", tiers=tiers_up_to(HealthSeverity.MAJOR))
        assert accepted.server.name == "ocp-mh-01"


class TestConcurrentRequests:
    async def test_parallel_requests_for_one_pattern_never_share_a_server(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, *(_server(i, name=f"ocp-par-{i:02d}") for i in range(6)))

        results = await asyncio.gather(
            *(_reserve(_service(mongo_holder), "ocp-par", workflow_id=f"wf-{i}") for i in range(6))
        )

        assert len({r.server.id for r in results}) == 6

    async def test_more_requests_than_servers_leave_the_extras_with_a_not_found(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(mongo_holder, *(_server(i, name=f"ocp-few-{i:02d}") for i in range(3)))

        outcomes = await asyncio.gather(
            *(_reserve(_service(mongo_holder), "ocp-few", workflow_id=f"wf-{i}") for i in range(8)),
            return_exceptions=True,
        )

        won = [o for o in outcomes if not isinstance(o, BaseException)]
        lost = [o for o in outcomes if isinstance(o, AvailableServersNotFoundError)]
        assert len(won) == 3
        assert len(lost) == 5
        assert len({o.server.id for o in won}) == 3

    async def test_different_patterns_run_side_by_side(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        await _seed(
            mongo_holder,
            *(_server(i, name=f"ocp-aaa-{i:02d}") for i in range(3)),
            *(_server(10 + i, name=f"ocp-bbb-{i:02d}") for i in range(3)),
        )

        results = await asyncio.gather(
            *(_reserve(_service(mongo_holder), "ocp-aaa", workflow_id=f"a{i}") for i in range(3)),
            *(_reserve(_service(mongo_holder), "ocp-bbb", workflow_id=f"b{i}") for i in range(3)),
        )

        assert len({r.server.id for r in results}) == 6


class TestLockIsHeldDuringTheLiveCheck:
    async def test_the_candidate_is_locked_with_a_probe_lease_while_the_vendor_is_called(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        server = _server(0, name="ocp-during-01", external_id="ext-during")
        await _seed(mongo_holder, server)
        seen: dict[str, object] = {}

        class _Watching(FakeProvider):
            async def get_one(self, identity: object) -> ProviderServer | None:
                stored = await _repo(mongo_holder).get_by_id(server.id)
                assert stored is not None
                seen["holder"] = stored.reservation.holder
                assert stored.reservation.expires_at is not None
                seen["lease"] = (stored.reservation.expires_at - utcnow()).total_seconds()
                seen["other_can_draw"] = await _repo(mongo_holder).count(
                    {"name": "ocp-during-01", **unreserved_filters(utcnow())}
                )
                return _live(server.name, server.identity.serial or "", "ext-during")

        provider = _Watching(seed=1, count=1, provider_type=ManagerType.REDFISH_STANDALONE.value)
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)

        await _reserve(service, "ocp-during")

        assert seen["holder"] == "install-server"
        assert seen["other_can_draw"] == 0
        # 15 s default vendor timeout + 60 s slack, floored at 120 s.
        assert 100 < float(seen["lease"]) <= 120  # ty: ignore[invalid-argument-type]


class TestOwnershipIsPerRequest:
    async def test_a_request_never_takes_over_another_requests_lock_with_the_same_fields(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        server = _server(0, name="ocp-token-01", external_id="ext-token")
        await _seed(mongo_holder, server)
        service = _service(mongo_holder)

        first = await _reserve(service, "ocp-token", workflow_id="same")
        with pytest.raises(AvailableServersNotFoundError):
            await _reserve(service, "ocp-token", workflow_id="same")

        stored = await _repo(mongo_holder).get_by_id(first.server.id)
        assert stored is not None
        assert stored.reservation.claim_token == first.server.reservation.claim_token

    async def test_a_claim_with_another_token_cannot_release_the_lock(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        server = _server(0, name="ocp-token-02", external_id="ext-token2")
        await _seed(mongo_holder, server)
        service = _service(mongo_holder)
        taken = await _reserve(service, "ocp-token", workflow_id="same")
        intruder = _claim("same")

        reservations = service._reservations
        assert await reservations.release_claim(taken.server.id, intruder)

        stored = await _repo(mongo_holder).get_by_id(taken.server.id)
        assert stored is not None
        assert stored.reservation.is_live()


class TestFinalCheckAndCleanup:
    async def test_a_server_put_into_maintenance_after_the_check_is_not_returned(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        server = _server(0, name="ocp-late-01", external_id="ext-late")
        await _seed(mongo_holder, server)

        class _Late(FakeProvider):
            async def get_one(self, identity: object) -> ProviderServer | None:
                stored = await _repo(mongo_holder).get_by_id(server.id)
                assert stored is not None
                return _live(server.name, server.identity.serial or "", "ext-late")

        provider = _Late(seed=1, count=1, provider_type=ManagerType.REDFISH_STANDALONE.value)
        service = _service(mongo_holder, provider_factory=lambda _mt: provider)
        reservations = service._reservations
        original = reservations.extend_claim

        async def _with_maintenance(*args: object, **kwargs: object) -> object:
            stored = await _repo(mongo_holder).get_by_id(server.id)
            assert stored is not None
            stored.maintenance.enabled = True
            stored.revision += 1
            await _repo(mongo_holder).upsert_with_revision_check(
                stored, expected_revision=stored.revision - 1
            )
            return await original(*args, **kwargs)  # ty: ignore[invalid-argument-type]

        reservations.extend_claim = _with_maintenance  # ty: ignore[invalid-assignment]

        with pytest.raises(AvailableServersNotFoundError):
            await _reserve(service, "ocp-late")

        stored = await _repo(mongo_holder).get_by_id(server.id)
        assert stored is not None
        assert not stored.reservation.is_live()

    async def test_a_failed_release_stops_the_request_from_locking_a_second_server(
        self, mongo_holder: MongoClientHolder
    ) -> None:
        for index in (0, 1):
            await _seed(
                mongo_holder,
                _server(index, name=f"ocp-stuck-0{index}", external_id=f"ext-stuck{index}"),
            )
        service = _service(mongo_holder, provider_factory=lambda _mt: _provider({}))

        async def _never_frees(*_args: object, **_kwargs: object) -> bool:
            return False

        service._reservations.release_claim = _never_frees  # ty: ignore[invalid-assignment]

        with pytest.raises(AvailableServersNotFoundError):
            await _reserve(service, "ocp-stuck")

        locked = await _repo(mongo_holder).count(
            {"name": {"$regex": "ocp-stuck"}, "reservation.holder": "install-server"}
        )
        assert locked == 1
