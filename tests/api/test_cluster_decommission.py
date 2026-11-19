"""`/api/v1/openshift/clusters` (docs/adr/0067): list jobs, retire a destroyed cluster's."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.api.test_auth import _auth_enabled_app
from tests.api.test_server_reservation import _installable_server

from app.application.services.cluster_decommission import ClusterDecommissionService
from app.config import get_settings
from app.domain.enums import OpenShiftState
from app.domain.models.audit_event import AuditEvent, EventType, Role
from app.domain.models.openshift import MembershipRun, OpenShiftLifecycle
from app.domain.models.server import Server
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.membership_run_repository import MongoMembershipRunRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.client import RedisClientHolder
from app.main import create_app
from app.utils.timeutil import utcnow

Ctx = tuple[AsyncClient, MongoServerRepository, MongoMembershipRunRepository, MongoClientHolder]


@pytest.fixture
async def ctx() -> AsyncIterator[Ctx]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        redis: RedisClientHolder = app.state.redis
        names = ("servers", "sites", "managers", "audit_events", "membership_runs")
        for name in names:
            await mongo.db[name].delete_many({})
        with contextlib.suppress(Exception):
            await redis.client.flushdb()
        yield (
            client,
            MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            MongoMembershipRunRepository(mongo),
            mongo,
        )
        for name in names:
            await mongo.db[name].delete_many({})


def _run(kind: str, name: str, *, ago: timedelta) -> MembershipRun:
    return MembershipRun(
        kind=kind,
        reported_by=name,
        finished_at=utcnow() - ago,
        duration_seconds=1.0,
        observed=3,
        matched=3,
        unmatched=0,
        partial=False,
    )


async def _installed(
    repo: MongoServerRepository, index: int, *, cluster: str, mce: str | None = None
) -> Server:
    server = _installable_server(index)
    server.openshift = OpenShiftLifecycle(
        lifecycle_state=OpenShiftState.INSTALLED, cluster_name=cluster, mce_name=mce
    )
    return await repo.upsert(server)


async def test_jobs_are_listed_silent_first_with_held_counts_and_the_seven_day_threshold(
    ctx: Ctx,
) -> None:
    client, repo, runs, _ = ctx
    await runs.record_run(_run("nodes", "ocp-live", ago=timedelta(minutes=10)))
    await runs.record_run(_run("nodes", "ocp-tomer-bat-yam", ago=timedelta(days=8)))
    await runs.record_run(_run("agents", "mce-old", ago=timedelta(days=7, minutes=1)))
    await _installed(repo, 1, cluster="ocp-tomer-bat-yam")
    await _installed(repo, 2, cluster="ocp-tomer-bat-yam")
    await _installed(repo, 3, cluster="ocp-live")

    body = (await client.get("/api/v1/openshift/clusters")).json()

    assert body["silent_after_seconds"] == 604800
    assert [(i["kind"], i["name"], i["held"], i["silent"]) for i in body["items"]] == [
        ("nodes", "ocp-tomer-bat-yam", 2, True),
        ("agents", "mce-old", 0, True),
        ("nodes", "ocp-live", 1, False),
    ]


async def test_just_inside_seven_days_is_still_reporting(ctx: Ctx) -> None:
    client, _, runs, _ = ctx
    await runs.record_run(_run("nodes", "ocp-quiet", ago=timedelta(days=6, hours=23)))
    (item,) = (await client.get("/api/v1/openshift/clusters")).json()["items"]
    assert item["silent"] is False


async def test_decommission_frees_its_servers_drops_its_record_and_audits(ctx: Ctx) -> None:
    client, repo, runs, mongo = ctx
    await runs.record_run(_run("nodes", "ocp-tomer-bat-yam", ago=timedelta(days=9)))
    await runs.record_run(_run("nodes", "ocp-live", ago=timedelta(minutes=5)))
    dead = [
        await _installed(repo, 1, cluster="ocp-tomer-bat-yam"),
        await _installed(repo, 2, cluster="ocp-tomer-bat-yam"),
    ]
    live = await _installed(repo, 3, cluster="ocp-live")

    resp = await client.post("/api/v1/openshift/clusters/nodes/ocp-tomer-bat-yam/decommission")

    assert resp.status_code == 200
    assert resp.json() == {"kind": "nodes", "name": "ocp-tomer-bat-yam", "freed": 2}
    for server in dead:
        stored = await repo.get_by_id(server.id)
        assert stored is not None
        assert stored.openshift.lifecycle_state is OpenShiftState.AVAILABLE
        assert stored.openshift.cluster_name is None
    kept = await repo.get_by_id(live.id)
    assert kept is not None
    assert kept.openshift.cluster_name == "ocp-live"
    assert [r.reported_by for r in await runs.list_all()] == ["ocp-live"]
    changed = await mongo.db["audit_events"].count_documents(
        {"event_type": "OPENSHIFT_STATE_CHANGED"}
    )
    assert changed == 2
    (event,) = (
        await mongo.db["audit_events"].find({"event_type": "CLUSTER_DECOMMISSIONED"}).to_list(5)
    )
    assert event["data"]["freed"] == 2
    assert event["data"]["name"] == "ocp-tomer-bat-yam"


async def test_an_agents_job_owns_servers_by_mce_not_by_cluster(ctx: Ctx) -> None:
    client, repo, runs, _ = ctx
    await runs.record_run(_run("agents", "mce-dead", ago=timedelta(days=9)))
    held = await _installed(repo, 1, cluster="hc-a", mce="mce-dead")
    other = await _installed(repo, 2, cluster="hc-a", mce="mce-live")

    assert (await client.post("/api/v1/openshift/clusters/agents/mce-dead/decommission")).json()[
        "freed"
    ] == 1

    freed, kept = await repo.get_by_id(held.id), await repo.get_by_id(other.id)
    assert freed is not None
    assert kept is not None
    assert freed.openshift.mce_name is None
    assert kept.openshift.mce_name == "mce-live"


async def test_the_list_cache_and_a_servers_detail_forget_the_old_membership(ctx: Ctx) -> None:
    client, repo, runs, _ = ctx
    await runs.record_run(_run("nodes", "ocp-gone", ago=timedelta(days=9)))
    server = await _installed(repo, 1, cluster="ocp-gone")
    assert (await client.get(f"/api/v1/servers/{server.id}")).json()["openshift"][
        "cluster_name"
    ] == "ocp-gone"
    assert (await client.get("/api/v1/servers/rows")).json()["items"][0][
        "cluster_name"
    ] == "ocp-gone"

    await client.post("/api/v1/openshift/clusters/nodes/ocp-gone/decommission")

    assert (await client.get(f"/api/v1/servers/{server.id}")).json()["openshift"][
        "cluster_name"
    ] is None
    assert (await client.get("/api/v1/servers/rows")).json()["items"][0]["cluster_name"] is None


async def test_an_unknown_job_is_404_and_a_bad_kind_422(ctx: Ctx) -> None:
    client, _, _, _ = ctx
    assert (
        await client.post("/api/v1/openshift/clusters/nodes/nope/decommission")
    ).status_code == 404
    assert (
        await client.post("/api/v1/openshift/clusters/vms/nope/decommission")
    ).status_code == 422


async def test_a_second_decommission_is_a_404(ctx: Ctx) -> None:
    client, _, runs, _ = ctx
    await runs.record_run(_run("nodes", "ocp-once", ago=timedelta(days=9)))
    assert (
        await client.post("/api/v1/openshift/clusters/nodes/ocp-once/decommission")
    ).status_code == 200
    assert (
        await client.post("/api/v1/openshift/clusters/nodes/ocp-once/decommission")
    ).status_code == 404


async def test_a_live_job_reclaims_after_a_mistaken_decommission(ctx: Ctx) -> None:
    from app.application.services.audit_service import AuditService
    from app.application.services.openshift_membership import OpenShiftMembershipService
    from app.domain.models.audit_event import Actor, ActorType
    from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
    from app.infrastructure.openshift.records import ClusterObservation

    client, repo, runs, mongo = ctx
    await runs.record_run(_run("nodes", "ocp-back", ago=timedelta(days=9)))
    server = await _installed(repo, 1, cluster="ocp-back")
    await client.post("/api/v1/openshift/clusters/nodes/ocp-back/decommission")

    service = OpenShiftMembershipService(
        server_repo=repo,
        audit=AuditService(repo=MongoAuditEventRepository(mongo)),
        actor=Actor(type=ActorType.SYSTEM, id="openshift:ocp-back"),
    )
    await service.reconcile(
        [
            ClusterObservation(
                hostname=server.name.lower(),
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp-back",
            )
        ],
        scope={"openshift.cluster_name": "ocp-back"},
        reported_by="ocp-back",
    )

    stored = await repo.get_by_id(server.id)
    assert stored is not None
    assert stored.openshift.lifecycle_state is OpenShiftState.INSTALLED


@pytest.mark.parametrize("role", [Role.VIEWER, Role.AUDITOR])
async def test_only_an_admin_may_list_or_decommission(role: Role) -> None:
    app = _auth_enabled_app(role)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        await client.post("/api/v1/auth/login", json={"username": "u", "password": "x"})
        assert (
            await client.post("/api/v1/openshift/clusters/nodes/x/decommission")
        ).status_code == 403
        assert (await client.get("/api/v1/openshift/clusters")).status_code == 403


def _service(
    repo: MongoServerRepository, runs: MongoMembershipRunRepository, mongo: MongoClientHolder
) -> ClusterDecommissionService:
    from app.application.services.audit_service import AuditService
    from app.domain.models.audit_event import Actor, ActorType
    from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository

    return ClusterDecommissionService(
        server_repo=repo,
        runs=runs,
        audit=AuditService(repo=MongoAuditEventRepository(mongo)),
        actor=Actor(type=ActorType.SYSTEM, id="cluster-watch"),
    )


async def _not_reporting_events(mongo: MongoClientHolder) -> list[dict]:
    return await mongo.db["audit_events"].find({"event_type": "CLUSTER_NOT_REPORTING"}).to_list(20)


async def test_a_job_going_silent_is_recorded_once_not_on_every_check(ctx: Ctx) -> None:
    _, repo, runs, mongo = ctx
    service = _service(repo, runs, mongo)
    await runs.record_run(_run("nodes", "ocp-tomer-bat-yam", ago=timedelta(days=8)))
    await runs.record_run(_run("nodes", "ocp-live", ago=timedelta(minutes=5)))
    await runs.record_run(_run("nodes", "ocp-quiet", ago=timedelta(days=6, hours=23)))
    await _installed(repo, 1, cluster="ocp-tomer-bat-yam")

    first = await service.notify_silent(now=utcnow(), silent_after_seconds=604800)
    again = await service.notify_silent(now=utcnow(), silent_after_seconds=604800)

    assert [j.name for j in first] == ["ocp-tomer-bat-yam"]
    assert again == []
    (event,) = await _not_reporting_events(mongo)
    assert event["data"]["name"] == "ocp-tomer-bat-yam"
    assert event["data"]["held"] == 1
    assert event["data"]["silent_seconds"] >= 8 * 86400
    assert event["actor"]["id"] == "cluster-watch"


async def test_two_replicas_checking_at_once_record_one_event(ctx: Ctx) -> None:
    import asyncio

    _, repo, runs, mongo = ctx
    await runs.record_run(_run("agents", "mce-dead", ago=timedelta(days=9)))

    await asyncio.gather(
        *(
            _service(repo, runs, mongo).notify_silent(now=utcnow(), silent_after_seconds=604800)
            for _ in range(5)
        )
    )

    assert len(await _not_reporting_events(mongo)) == 1


async def test_a_cluster_that_reports_again_and_goes_silent_again_is_recorded_again(
    ctx: Ctx,
) -> None:
    _, repo, runs, mongo = ctx
    service = _service(repo, runs, mongo)
    await runs.record_run(_run("nodes", "ocp-flaky", ago=timedelta(days=8)))
    await service.notify_silent(now=utcnow(), silent_after_seconds=604800)

    await runs.record_run(_run("nodes", "ocp-flaky", ago=timedelta(minutes=1)))
    assert await service.notify_silent(now=utcnow(), silent_after_seconds=604800) == []

    await runs.record_run(_run("nodes", "ocp-flaky", ago=timedelta(days=8)))
    assert [
        j.name for j in await service.notify_silent(now=utcnow(), silent_after_seconds=604800)
    ] == ["ocp-flaky"]
    assert len(await _not_reporting_events(mongo)) == 2


async def test_a_failed_event_write_is_retried_on_the_next_check(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.services.audit_service import AuditService

    _, repo, runs, mongo = ctx
    service = _service(repo, runs, mongo)
    await runs.record_run(_run("nodes", "ocp-retry", ago=timedelta(days=8)))
    real = AuditService.record
    calls = 0

    async def flaky(self: AuditService, event_type: EventType, **kwargs: Any) -> AuditEvent:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("audit down")
        return await real(self, event_type, **kwargs)

    monkeypatch.setattr(AuditService, "record", flaky)

    with pytest.raises(RuntimeError):
        await service.notify_silent(now=utcnow(), silent_after_seconds=604800)
    assert [
        j.name for j in await service.notify_silent(now=utcnow(), silent_after_seconds=604800)
    ] == ["ocp-retry"]
    assert len(await _not_reporting_events(mongo)) == 1
