"""Personal API tokens (docs/adr/0053): issue, use, replace, revoke, and who they act as."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from tests.api.test_auth import _auth_enabled_app

from app.domain.models.audit_event import Role
from app.infrastructure.mongodb.api_token_repository import MongoApiTokenRepository
from app.utils.timeutil import utcnow

_USER = {"username": "tokentester", "password": "x"}


@pytest.fixture(params=[Role.ADMIN, Role.VIEWER], ids=["admin", "viewer"])
async def signed_in(request: pytest.FixtureRequest) -> AsyncIterator[tuple[AsyncClient, Role]]:
    """A client with a session cookie for a user holding `request.param`."""
    app = _auth_enabled_app(request.param)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        await MongoApiTokenRepository(app.state.mongo).delete("tokentester")
        assert (await client.post("/api/v1/auth/login", json=_USER)).status_code == 200
        yield client, request.param
        await MongoApiTokenRepository(app.state.mongo).delete("tokentester")


async def _bearer(client: AsyncClient, token: str, path: str) -> int:
    """GET `path` with only the bearer token (no session cookie)."""
    async with AsyncClient(transport=client._transport, base_url="http://test") as bare:
        return (await bare.get(path, headers={"Authorization": f"Bearer {token}"})).status_code


async def test_a_token_is_shown_once_and_works_as_the_user(
    signed_in: tuple[AsyncClient, Role],
) -> None:
    client, role = signed_in
    created = (await client.post("/api/v1/auth/token")).json()

    assert created["token"].startswith("ssk_")
    assert created["info"]["role"] == ("VIEWER" if role is Role.VIEWER else "AUDITOR")
    assert "token" not in (await client.get("/api/v1/auth/token")).json()
    assert await _bearer(client, created["token"], "/api/v1/servers?page_size=1") == 200


async def test_a_token_never_reaches_admin_rights(signed_in: tuple[AsyncClient, Role]) -> None:
    client, _ = signed_in
    token = (await client.post("/api/v1/auth/token")).json()["token"]

    async with AsyncClient(transport=client._transport, base_url="http://test") as bare:
        resp = await bare.put(
            "/api/v1/servers/none/maintenance",
            json={"enabled": True, "reason": "x"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 403


async def test_a_token_cannot_issue_a_token(signed_in: tuple[AsyncClient, Role]) -> None:
    client, _ = signed_in
    token = (await client.post("/api/v1/auth/token")).json()["token"]

    async with AsyncClient(transport=client._transport, base_url="http://test") as bare:
        resp = await bare.post("/api/v1/auth/token", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403


async def test_a_new_token_replaces_the_old_one(signed_in: tuple[AsyncClient, Role]) -> None:
    client, _ = signed_in
    first = (await client.post("/api/v1/auth/token")).json()["token"]
    second = (await client.post("/api/v1/auth/token")).json()["token"]

    assert await _bearer(client, first, "/api/v1/sites") == 401
    assert await _bearer(client, second, "/api/v1/sites") == 200


async def test_a_revoked_token_stops_working(signed_in: tuple[AsyncClient, Role]) -> None:
    client, _ = signed_in
    token = (await client.post("/api/v1/auth/token")).json()["token"]

    assert (await client.delete("/api/v1/auth/token")).status_code == 204

    assert await _bearer(client, token, "/api/v1/sites") == 401
    assert (await client.get("/api/v1/auth/token")).json() is None


async def test_an_action_by_token_is_recorded_as_its_owner(
    signed_in: tuple[AsyncClient, Role],
) -> None:
    client, role = signed_in
    await client.post("/api/v1/auth/token")

    if role is Role.VIEWER:
        pytest.skip("viewers cannot read the audit trail")
    events = (await client.get("/api/v1/events?event_type=TOKEN_CREATED&page_size=5")).json()

    assert any(e["actor"]["id"] == "tokentester" for e in events["items"])


async def test_login_off_means_no_token_is_needed() -> None:
    from app.main import create_app

    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        assert (await client.get("/api/v1/auth/token")).status_code == 404


async def test_an_expired_token_is_refused_even_beside_a_valid_session(
    signed_in: tuple[AsyncClient, Role],
) -> None:
    client, _ = signed_in
    token = (await client.post("/api/v1/auth/token")).json()["token"]
    repo = MongoApiTokenRepository(client._transport.app.state.mongo)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    stored = await repo.get("tokentester")
    assert stored is not None
    await repo.replace(stored.model_copy(update={"expires_at": utcnow() - timedelta(seconds=1)}))

    resp = await client.get("/api/v1/sites", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401


async def test_the_secret_is_never_stored(signed_in: tuple[AsyncClient, Role]) -> None:
    client, _ = signed_in
    token = (await client.post("/api/v1/auth/token")).json()["token"]
    repo = MongoApiTokenRepository(client._transport.app.state.mongo)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    stored = await repo.get("tokentester")

    assert stored is not None
    assert token not in stored.model_dump_json()
    assert stored.token_hash != token


async def test_only_an_admin_lists_or_revokes_other_peoples_tokens(
    signed_in: tuple[AsyncClient, Role],
) -> None:
    client, role = signed_in
    await client.post("/api/v1/auth/token")

    listed = await client.get("/api/v1/auth/tokens")
    revoked = await client.delete("/api/v1/auth/tokens/tokentester")

    assert (listed.status_code, revoked.status_code) == (
        (200, 204) if role is Role.ADMIN else (403, 403)
    )


async def test_listing_tokens_with_login_off_is_not_found() -> None:
    from app.main import create_app

    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        assert (await client.get("/api/v1/auth/tokens")).status_code == 404
