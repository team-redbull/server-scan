"""`AuthService.authenticate` — the priority rule from docs/adr/0034 /
the operator's own AD-integration spec: credentials first, then admin
(user list, then groups) before viewer (user list, then groups),
case-insensitive throughout, infra failures always propagate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.application.services.auth_service as auth_service_module
from app.application.services.auth_service import AuthService
from app.config.settings import Settings
from app.domain.models.audit_event import Role
from app.domain.services.authz import LoginResult
from app.errors import ServiceUnavailableError

pytestmark = pytest.mark.unit


class _FakeAdApi:
    """Records which groups were queried; membership comes from `groups`."""

    def __init__(self, groups: dict[str, set[str]] | None = None) -> None:
        self.groups = groups or {}
        self.queried: list[str] = []

    async def group_members(self, group_sam: str) -> set[str]:
        self.queried.append(group_sam)
        return self.groups.get(group_sam, set())


class _DownAdApi:
    async def group_members(self, group_sam: str) -> set[str]:
        raise ServiceUnavailableError("AD API is down")


def _settings(
    *,
    admin_groups: str = "Admins",
    view_groups: str = "Viewers",
    admin_users: str = "",
    viewer_users: str = "",
) -> Settings:
    return Settings(
        _env_file=None,
        ldap_server="dc.example.com",
        ldap_domain="EXAMPLE",
        ad_api_url="https://ad-api.example.com",
        admin_groups=admin_groups,
        view_groups=view_groups,
        admin_users=admin_users,
        viewer_users=viewer_users,
    )


@pytest.fixture(autouse=True)
def _stub_ldap_validate(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Every test in this file exercises role resolution, not the LDAP bind
    itself (`tests/unit/infrastructure/ad/test_client.py` covers that) — the
    bind outcome comes from the `bind_ok` marker a test sets via `_bind`.
    """
    request.node._bind_ok = True

    async def _fake(_settings: Settings, _username: str, _password: str) -> bool:
        return request.node._bind_ok

    monkeypatch.setattr(auth_service_module, "ldap_validate", _fake)


def _bind(request: pytest.FixtureRequest, *, ok: bool) -> None:
    request.node._bind_ok = ok


class TestCredentialsCheckedFirst:
    async def test_wrong_password_returns_none_before_any_group_check(
        self, request: pytest.FixtureRequest
    ) -> None:
        _bind(request, ok=False)
        ad_api = _FakeAdApi({"admins": {"jdoe"}})
        service = AuthService(_settings(), ad_api)
        assert await service.authenticate("jdoe", "wrong") is None
        assert ad_api.queried == []


class TestAdminBeforeViewer:
    async def test_admin_group_member_is_admin(self) -> None:
        ad_api = _FakeAdApi({"admins": {"jdoe"}})
        service = AuthService(_settings(), ad_api)
        assert await service.authenticate("jdoe", "x") is Role.ADMIN

    async def test_member_of_both_admin_and_viewer_group_is_admin(self) -> None:
        ad_api = _FakeAdApi({"admins": {"jdoe"}, "viewers": {"jdoe"}})
        service = AuthService(_settings(), ad_api)
        assert await service.authenticate("jdoe", "x") is Role.ADMIN

    async def test_viewer_group_member_only_is_viewer(self) -> None:
        ad_api = _FakeAdApi({"admins": set(), "viewers": {"jdoe"}})
        service = AuthService(_settings(), ad_api)
        assert await service.authenticate("jdoe", "x") is Role.VIEWER

    async def test_listed_nowhere_is_no_permission(self) -> None:
        ad_api = _FakeAdApi({"admins": set(), "viewers": set()})
        service = AuthService(_settings(), ad_api)
        assert await service.authenticate("jdoe", "x") is LoginResult.NO_PERMISSION

    async def test_second_admin_group_matches(self) -> None:
        ad_api = _FakeAdApi({"admins-a": set(), "admins-b": {"jdoe"}})
        service = AuthService(_settings(admin_groups="Admins-A,Admins-B"), ad_api)
        assert await service.authenticate("jdoe", "x") is Role.ADMIN


class TestUserListsSkipTheApiCall:
    async def test_admin_user_never_queries_any_group(self) -> None:
        ad_api = _FakeAdApi()
        service = AuthService(_settings(admin_users="jdoe"), ad_api)
        assert await service.authenticate("jdoe", "x") is Role.ADMIN
        assert ad_api.queried == []

    async def test_viewer_user_queries_only_the_admin_groups_first(self) -> None:
        ad_api = _FakeAdApi({"admins": set()})
        service = AuthService(_settings(viewer_users="jdoe"), ad_api)
        assert await service.authenticate("jdoe", "x") is Role.VIEWER
        assert ad_api.queried == ["admins"]


class TestCaseInsensitive:
    async def test_username_matches_admin_users_regardless_of_case(self) -> None:
        ad_api = _FakeAdApi()
        service = AuthService(_settings(admin_users="JDoe"), ad_api)
        assert await service.authenticate("jDOE", "x") is Role.ADMIN

    async def test_group_membership_matches_regardless_of_case(self) -> None:
        ad_api = _FakeAdApi({"admins": {"jdoe"}})
        service = AuthService(_settings(), ad_api)
        assert await service.authenticate("JDoe", "x") is Role.ADMIN


class TestInfrastructureFailurePropagates:
    async def test_a_dead_ad_api_raises_not_none_and_not_no_permission(self) -> None:
        service = AuthService(_settings(), _DownAdApi())
        with pytest.raises(ServiceUnavailableError):
            await service.authenticate("jdoe", "x")


class TestLiveFileOverridesTheStaticList:
    """The mounted-ConfigMap-volume path (deploy/README.md, "Configuration
    notes") — re-read on every login, so a changed group list needs no
    API pod restart.
    """

    async def test_a_configured_file_is_preferred_over_the_static_field(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "admin-groups"
        path.write_text("Admins-From-File")
        settings = _settings(admin_groups="Admins-From-Env")
        settings = settings.model_copy(update={"admin_groups_file": str(path)})
        ad_api = _FakeAdApi({"admins-from-file": {"jdoe"}})
        service = AuthService(settings, ad_api)
        assert await service.authenticate("jdoe", "x") is Role.ADMIN
        assert ad_api.queried == ["admins-from-file"]

    async def test_an_unset_file_field_falls_back_to_the_static_value(self) -> None:
        settings = _settings(admin_groups="Admins")
        ad_api = _FakeAdApi({"admins": {"jdoe"}})
        service = AuthService(settings, ad_api)
        assert await service.authenticate("jdoe", "x") is Role.ADMIN
        assert ad_api.queried == ["admins"]

    async def test_a_missing_file_falls_back_to_the_static_value_rather_than_erroring(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(admin_groups="Admins")
        settings = settings.model_copy(
            update={"admin_groups_file": str(tmp_path / "does-not-exist")}
        )
        ad_api = _FakeAdApi({"admins": {"jdoe"}})
        service = AuthService(settings, ad_api)
        assert await service.authenticate("jdoe", "x") is Role.ADMIN
        assert ad_api.queried == ["admins"]

    async def test_a_file_update_is_visible_on_the_very_next_login(self, tmp_path: Path) -> None:
        path = tmp_path / "admin-users"
        path.write_text("jdoe")
        settings = _settings().model_copy(update={"admin_users_file": str(path)})
        service = AuthService(settings, _FakeAdApi())
        assert await service.authenticate("jdoe", "x") is Role.ADMIN

        path.write_text("someone-else")
        assert await service.authenticate("jdoe", "x") is LoginResult.NO_PERMISSION
