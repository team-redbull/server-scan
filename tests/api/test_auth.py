"""API tests for AD login (docs/adr/0034): `POST /auth/login`,
`POST /auth/logout`, `GET /auth/me`, the dev bypass when
`auth_enabled=False`, and that `require_admin` actually gates the
maintenance endpoints once a session or token exists.

`_auth_service` is overridden with a fake whose `authenticate` result is
fixed per test — the LDAP bind and AD API calls themselves are covered by
`tests/unit/infrastructure/ad/test_client.py` and
`tests/unit/application/services/test_auth_service.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.auth import _auth_service
from app.config import get_settings
from app.config.settings import Settings
from app.domain.enums import HealthSeverity, InstallationType, Vendor
from app.domain.models.audit_event import Role
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.server import Identity, Server
from app.domain.services.authz import LoginResult
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.errors import ServiceUnavailableError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


def _make_server(name: str, *, index: int = 0) -> Server:
    now = utcnow()
    serial = f"AUTHAPI{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=Vendor.CISCO,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"auth-api-uuid-{index:06d}",
        ),
        classification=Classification(installation_type=InstallationType.UNCLASSIFIED),
        health=Health(overall=HealthSeverity.HEALTHY, connectivity=HealthSeverity.HEALTHY),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


class _FakeAuthService:
    """Stands in for `AuthService`: `authenticate` always returns (or raises) `result`."""

    def __init__(self, result: object) -> None:
        self._result = result

    async def authenticate(self, _username: str, _password: str) -> object:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _auth_enabled_app(login_result: object, **settings_overrides: object) -> FastAPI:
    settings = get_settings().model_copy(
        update={
            "auth_enabled": True,
            "ldap_server": "dc.example.com",
            "ldap_domain": "EXAMPLE",
            "ad_api_url": "https://ad-api.example.com",
            "ad_api_client_id": SecretStr("test-client-id"),
            **settings_overrides,
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_auth_service] = lambda: _FakeAuthService(login_result)
    return app


@pytest.fixture
async def disabled_client() -> AsyncIterator[AsyncClient]:
    """The default: `auth_enabled=False`, exactly today's dev/test config."""
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


@pytest.fixture(params=[Role.ADMIN])
async def enabled_client(request: pytest.FixtureRequest) -> AsyncIterator[AsyncClient]:
    """`auth_enabled=True`, login always resolving to `request.param`."""
    app = _auth_enabled_app(request.param)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


class TestDevBypass:
    async def test_me_is_auto_admin_with_no_login_page(self, disabled_client: AsyncClient) -> None:
        resp = await disabled_client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json() == {
            "login_required": False,
            "authenticated": True,
            "username": "dev",
            "role": "ADMIN",
        }

    async def test_reads_work_with_no_session_at_all(self, disabled_client: AsyncClient) -> None:
        assert (await disabled_client.get("/api/v1/servers")).status_code == 200


class TestMeWithAuthEnabled:
    async def test_no_cookie_is_not_authenticated(self) -> None:
        app = _auth_enabled_app(LoginResult.NO_PERMISSION)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            resp = await client.get("/api/v1/auth/me")
            assert resp.json() == {
                "login_required": True,
                "authenticated": False,
                "username": None,
                "role": None,
            }

    async def test_a_read_with_no_session_is_401(self) -> None:
        app = _auth_enabled_app(LoginResult.NO_PERMISSION)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            resp = await client.get("/api/v1/servers")
            assert resp.status_code == 401
            assert resp.json()["code"] == "UNAUTHORIZED"


class TestLogin:
    async def test_success_sets_cookie_and_me_reflects_it(self) -> None:
        app = _auth_enabled_app(Role.ADMIN)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            resp = await client.post(
                "/api/v1/auth/login", json={"username": "Admin.User", "password": "x"}
            )
            assert resp.status_code == 200
            assert resp.json() == {"username": "admin.user", "role": "ADMIN"}
            assert "server_scan_session" in resp.cookies

            me = (await client.get("/api/v1/auth/me")).json()
            assert me == {
                "login_required": True,
                "authenticated": True,
                "username": "admin.user",
                "role": "ADMIN",
            }

    async def test_wrong_password_is_401(self) -> None:
        app = _auth_enabled_app(None)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            resp = await client.post(
                "/api/v1/auth/login", json={"username": "u", "password": "wrong"}
            )
            assert resp.status_code == 401
            assert resp.json()["code"] == "UNAUTHORIZED"
            assert "server_scan_session" not in resp.cookies

    async def test_no_permission_is_403(self) -> None:
        app = _auth_enabled_app(LoginResult.NO_PERMISSION)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            resp = await client.post("/api/v1/auth/login", json={"username": "u", "password": "x"})
            assert resp.status_code == 403
            assert resp.json()["code"] == "FORBIDDEN"

    async def test_directory_down_is_503_never_401(self) -> None:
        """The spec's own headline rule: an infra failure must never look like a wrong password."""
        app = _auth_enabled_app(ServiceUnavailableError("AD API is down"))
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            resp = await client.post("/api/v1/auth/login", json={"username": "u", "password": "x"})
            assert resp.status_code == 503
            assert resp.json()["code"] == "SERVICE_UNAVAILABLE"

    async def test_logout_clears_the_session(self) -> None:
        app = _auth_enabled_app(Role.ADMIN)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            await client.post("/api/v1/auth/login", json={"username": "u", "password": "x"})
            assert (await client.get("/api/v1/auth/me")).json()["authenticated"] is True

            logout = await client.post("/api/v1/auth/logout")
            assert logout.status_code == 204
            assert (await client.get("/api/v1/auth/me")).json()["authenticated"] is False


class TestMaintenanceRoleGate:
    """`PUT`/`DELETE .../maintenance` require `Role.ADMIN` — a viewer session
    or token reads everything but can't toggle it.
    """

    async def test_viewer_session_gets_403_admin_session_gets_200(self) -> None:
        viewer_app = _auth_enabled_app(Role.VIEWER)
        admin_app = _auth_enabled_app(Role.ADMIN)
        async with (
            AsyncClient(transport=ASGITransport(app=viewer_app), base_url="http://test") as viewer,
            viewer_app.router.lifespan_context(viewer_app),
            AsyncClient(transport=ASGITransport(app=admin_app), base_url="http://test") as admin,
            admin_app.router.lifespan_context(admin_app),
        ):
            settings: Settings = get_settings()
            mongo: MongoClientHolder = viewer_app.state.mongo
            await mongo.db["servers"].delete_many({})
            repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
            server = await repo.upsert(_make_server("srv-role-gate"))

            await viewer.post("/api/v1/auth/login", json={"username": "v", "password": "x"})
            viewer_resp = await viewer.put(
                f"/api/v1/servers/{server.id}/maintenance", json={"reason": "x"}
            )
            assert viewer_resp.status_code == 403

            await admin.post("/api/v1/auth/login", json={"username": "a", "password": "x"})
            admin_resp = await admin.put(
                f"/api/v1/servers/{server.id}/maintenance", json={"reason": "x"}
            )
            assert admin_resp.status_code == 200
            assert admin_resp.json()["maintenance"]["enabled"] is True

            await mongo.db["servers"].delete_many({})

    async def test_viewer_can_still_read_and_filter_by_maintenance(self) -> None:
        viewer_app = _auth_enabled_app(Role.VIEWER)
        async with (
            AsyncClient(transport=ASGITransport(app=viewer_app), base_url="http://test") as viewer,
            viewer_app.router.lifespan_context(viewer_app),
        ):
            await viewer.post("/api/v1/auth/login", json={"username": "v", "password": "x"})
            resp = await viewer.get("/api/v1/servers?maintenance=true")
            assert resp.status_code == 200


class TestApiTokens:
    async def test_admin_token_can_write_viewer_token_cannot(self) -> None:
        app = _auth_enabled_app(
            LoginResult.NO_PERMISSION,
            api_token_admin=SecretStr("admin-token"),
            api_token_viewer=SecretStr("viewer-token"),
        )
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            settings: Settings = get_settings()
            mongo: MongoClientHolder = app.state.mongo
            await mongo.db["servers"].delete_many({})
            repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
            server = await repo.upsert(_make_server("srv-token-gate"))

            viewer_headers = {"Authorization": "Bearer viewer-token"}
            assert (await client.get("/api/v1/servers", headers=viewer_headers)).status_code == 200
            viewer_write = await client.put(
                f"/api/v1/servers/{server.id}/maintenance",
                json={"reason": "x"},
                headers=viewer_headers,
            )
            assert viewer_write.status_code == 403

            admin_headers = {"Authorization": "Bearer admin-token"}
            admin_write = await client.put(
                f"/api/v1/servers/{server.id}/maintenance",
                json={"reason": "x"},
                headers=admin_headers,
            )
            assert admin_write.status_code == 200

            await mongo.db["servers"].delete_many({})

    async def test_an_unrecognized_token_is_401(self) -> None:
        app = _auth_enabled_app(LoginResult.NO_PERMISSION, api_token_admin=SecretStr("admin-token"))
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            resp = await client.get(
                "/api/v1/servers", headers={"Authorization": "Bearer not-a-real-token"}
            )
            assert resp.status_code == 401
