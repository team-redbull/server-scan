"""`/api/v1/jobs` (docs/adr/0055): admin only, 404/409 rules, audit event, logs."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from tests.api.test_auth import _auth_enabled_app

from app.domain.models.audit_event import Role
from app.infrastructure.mongodb.api_token_repository import MongoApiTokenRepository
from app.main import create_app

ONEVIEW = "server-scan-collector-oneview"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """auth off (everyone is the dev admin), fake Kubernetes backend."""
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


@pytest.mark.parametrize("role", [Role.VIEWER, Role.AUDITOR])
async def test_only_an_admin_may_use_every_route(role: Role) -> None:
    app = _auth_enabled_app(role)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        assert (
            await client.post("/api/v1/auth/login", json={"username": "u", "password": "x"})
        ).status_code == 200
        assert (await client.get("/api/v1/jobs")).status_code == 403
        assert (await client.post(f"/api/v1/jobs/{ONEVIEW}/run")).status_code == 403
        logs = f"/api/v1/jobs/{ONEVIEW}/runs/j/pods/p/logs"
        assert (await client.get(logs)).status_code == 403


async def test_a_personal_token_cannot_use_it() -> None:
    app = _auth_enabled_app(Role.ADMIN)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        await client.post("/api/v1/auth/login", json={"username": "tokentester", "password": "x"})
        token = (await client.post("/api/v1/auth/token")).json()["token"]
        async with AsyncClient(transport=client._transport, base_url="http://test") as bare:
            resp = await bare.get("/api/v1/jobs", headers={"Authorization": f"Bearer {token}"})
        await MongoApiTokenRepository(app.state.mongo).delete("tokentester")
        assert resp.status_code == 403


async def test_list_shows_the_cronjobs(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/jobs")).json()

    assert body["enabled"] is True
    assert body["backend"] == "fake"
    assert len(body["cronjobs"]) == 9
    assert sorted(c["enabled"] for c in body["cronjobs"]).count(False) == 2


async def test_run_creates_a_job_audits_it_and_a_second_run_is_409(client: AsyncClient) -> None:
    name = "server-scan-collector-fake"
    first = await client.post(f"/api/v1/jobs/{name}/run")

    assert first.status_code == 201
    job = first.json()
    assert job["state"] == "active"
    assert job["name"].startswith(f"{name}-manual-")
    second = await client.post(f"/api/v1/jobs/{name}/run")
    assert second.status_code == 409
    listed = {c["name"]: c for c in (await client.get("/api/v1/jobs")).json()["cronjobs"]}
    assert listed[name]["active"] is True
    events = (await client.get("/api/v1/events?event_type=JOB_TRIGGERED&page_size=50")).json()
    assert any(
        e["data"].get("job") == job["name"] and e["data"]["cronjob"] == name
        for e in events["items"]
    )


async def test_a_cronjob_outside_the_allow_list_is_404(client: AsyncClient) -> None:
    for name in ("server-scan-nodes-status-nodes", "anything-else"):
        assert (await client.post(f"/api/v1/jobs/{name}/run")).status_code == 404


async def test_logs_are_served_only_for_a_pod_of_the_cronjobs_job(client: AsyncClient) -> None:
    name = "server-scan-prune-events"
    cj = next(c for c in (await client.get("/api/v1/jobs")).json()["cronjobs"] if c["name"] == name)
    job = cj["jobs"][0]
    pod = job["pods"][0]["name"]

    ok = await client.get(f"/api/v1/jobs/{name}/runs/{job['name']}/pods/{pod}/logs?tail=3")
    assert ok.status_code == 200
    assert len(ok.json()["text"].strip().splitlines()) <= 3
    wrong = await client.get(f"/api/v1/jobs/{ONEVIEW}/runs/{job['name']}/pods/{pod}/logs")
    assert wrong.status_code == 404
