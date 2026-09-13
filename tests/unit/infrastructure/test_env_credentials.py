"""
`app.infrastructure.credentials.env` — where a collector learns a vendor
manager's address and login. A half-configured vendor must fail naming the
missing variables; `UCS_MANAGER` alone has a login but no endpoint (CLAUDE.md).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.infrastructure.credentials.env import (
    EnvConnectionResolver,
    configured_manager_types,
    resolve_login,
)

pytestmark = pytest.mark.unit


def _settings(**overrides: Any) -> Settings:
    # `_env_file=None` so a developer's real .env can't leak into the test.
    return Settings(_env_file=None, **overrides)


CENTRAL = {
    "ucs_central_ip": "10.0.0.1",
    "ucs_central_username": "central-admin",
    "ucs_central_password": "s3cret",
}


class TestResolve:
    def test_returns_the_configured_connection(self) -> None:
        connection = EnvConnectionResolver(_settings(**CENTRAL)).resolve(ManagerType.UCS_CENTRAL)
        assert connection.endpoint == "10.0.0.1"
        assert connection.username == "central-admin"
        assert connection.password == "s3cret"

    @pytest.mark.parametrize(
        ("manager_type", "values"),
        [
            (ManagerType.UCS_CENTRAL, {"ucs_central_ip": "10.0.0.1"}),
            (ManagerType.ONEVIEW, {"oneview_ip": "10.0.0.2"}),
            (ManagerType.OPENMANAGE, {"ome_ip": "10.0.0.3"}),
        ],
    )
    def test_every_vendor_has_its_own_variables(
        self, manager_type: ManagerType, values: dict[str, str]
    ) -> None:
        prefix = next(iter(values)).rsplit("_", 1)[0]
        settings = _settings(**values, **{f"{prefix}_username": "u", f"{prefix}_password": "p"})
        assert EnvConnectionResolver(settings).resolve(manager_type).endpoint == next(
            iter(values.values())
        )

    def test_nothing_configured_names_every_missing_variable(self) -> None:
        with pytest.raises(ManagerNotConfiguredError) as exc:
            EnvConnectionResolver(_settings()).resolve(ManagerType.UCS_CENTRAL)
        message = str(exc.value)
        assert "INVENTORY_UCS_CENTRAL_IP" in message
        assert "INVENTORY_UCS_CENTRAL_USERNAME" in message
        assert "INVENTORY_UCS_CENTRAL_PASSWORD" in message

    @pytest.mark.parametrize(
        "missing", ["ucs_central_ip", "ucs_central_username", "ucs_central_password"]
    )
    def test_a_partially_configured_vendor_is_a_configuration_error(self, missing: str) -> None:
        """Half-configured must not reach the vendor. A blank password is
        a real login attempt that fails as bad credentials, which sends
        an operator hunting a password problem that does not exist.
        """
        values = {**CENTRAL, missing: ""}
        with pytest.raises(ManagerNotConfiguredError) as exc:
            EnvConnectionResolver(_settings(**values)).resolve(ManagerType.UCS_CENTRAL)
        assert f"INVENTORY_{missing.upper()}" in str(exc.value)

    def test_whitespace_only_counts_as_missing(self) -> None:
        with pytest.raises(ManagerNotConfiguredError):
            EnvConnectionResolver(_settings(**{**CENTRAL, "ucs_central_password": "   "})).resolve(
                ManagerType.UCS_CENTRAL
            )

    def test_surrounding_whitespace_is_stripped(self) -> None:
        settings = _settings(**{**CENTRAL, "ucs_central_ip": "  10.0.0.1  "})
        assert EnvConnectionResolver(settings).resolve(ManagerType.UCS_CENTRAL).endpoint == (
            "10.0.0.1"
        )

    def test_intersight_uses_the_same_three_fields(self) -> None:
        """Intersight signs requests with an API key rather than logging
        in, but reuses the same shape so the values file and Secret stay
        uniform: username carries the API Key ID, password the secret key.
        """
        settings = _settings(
            intersight_ip="intersight.com",
            intersight_api_key_id="key-id-123",
            intersight_api_key_pem="-----BEGIN EC PRIVATE KEY-----",
        )
        connection = EnvConnectionResolver(settings).resolve(ManagerType.INTERSIGHT)
        assert connection.endpoint == "intersight.com"
        assert connection.username == "key-id-123"

    def test_every_known_manager_type_is_configurable(self) -> None:
        """Guards against a `ManagerType` being added without anywhere to
        put its connection details — which would surface only when
        someone tried to run its collector.
        """
        for manager_type in ManagerType:
            try:
                EnvConnectionResolver(_settings()).resolve(manager_type)
            except ManagerNotConfiguredError as exc:
                assert "INVENTORY_" in str(exc), f"{manager_type} has no settings fields"


class TestUcsManagerHasNoEndpointOfItsOwn:
    """The message is the whole feature: `--manager-type UCS_MANAGER` must say
    where the domain credentials go and which collector to run instead.
    """

    def test_resolve_refuses_ucs_manager_and_points_at_ucs_central(self) -> None:
        with pytest.raises(ManagerNotConfiguredError) as exc:
            EnvConnectionResolver(_settings()).resolve(ManagerType.UCS_MANAGER)
        message = str(exc.value)
        assert "UCS Central" in message
        assert "INVENTORY_UCS_CENTRAL_IP" in message
        assert "INVENTORY_UCS_MANAGER_USERNAME" in message
        assert "--manager-type UCS_CENTRAL" in message

    def test_refusal_does_not_depend_on_what_is_configured(self) -> None:
        """It is a fact about the product, not about this deployment: a UCS
        Manager domain has no address to configure even when every other
        value is filled in.
        """
        settings = _settings(
            **CENTRAL, ucs_manager_username="domain-admin", ucs_manager_password="domain-secret"
        )
        with pytest.raises(ManagerNotConfiguredError):
            EnvConnectionResolver(settings).resolve(ManagerType.UCS_MANAGER)

    def test_there_is_no_ucs_manager_ip_setting_left(self) -> None:
        """Deleted rather than ignored — leaving it would let a deployment
        keep setting a variable that silently does nothing.
        """
        assert "ucs_manager_ip" not in Settings.model_fields


class TestResolveLogin:
    def test_returns_the_fleet_wide_domain_login(self) -> None:
        """One UCS Manager service account, used against every domain
        Central reports — not a login per domain.
        """
        settings = _settings(
            ucs_manager_username="domain-admin", ucs_manager_password="domain-secret"
        )
        assert resolve_login(settings, ManagerType.UCS_MANAGER) == (
            "domain-admin",
            "domain-secret",
        )

    @pytest.mark.parametrize("missing", ["ucs_manager_username", "ucs_manager_password"])
    def test_a_missing_half_names_the_variable_to_set(self, missing: str) -> None:
        values = {
            "ucs_manager_username": "domain-admin",
            "ucs_manager_password": "domain-secret",
            missing: "",
        }
        with pytest.raises(ManagerNotConfiguredError) as exc:
            resolve_login(_settings(**values), ManagerType.UCS_MANAGER)
        message = str(exc.value)
        assert f"INVENTORY_{missing.upper()}" in message
        # The IP genuinely is not needed here, so naming it would send the
        # operator to invent a value that is never used.
        assert "INVENTORY_UCS_MANAGER_IP" not in message

    def test_nothing_configured_names_both(self) -> None:
        with pytest.raises(ManagerNotConfiguredError) as exc:
            resolve_login(_settings(), ManagerType.UCS_MANAGER)
        message = str(exc.value)
        assert "INVENTORY_UCS_MANAGER_USERNAME" in message
        assert "INVENTORY_UCS_MANAGER_PASSWORD" in message

    def test_whitespace_only_counts_as_missing(self) -> None:
        settings = _settings(ucs_manager_username="domain-admin", ucs_manager_password="   ")
        with pytest.raises(ManagerNotConfiguredError):
            resolve_login(settings, ManagerType.UCS_MANAGER)

    def test_works_for_a_vendor_that_also_has_an_endpoint(self) -> None:
        assert resolve_login(_settings(**CENTRAL), ManagerType.UCS_CENTRAL) == (
            "central-admin",
            "s3cret",
        )


class TestConfiguredManagerTypes:
    def test_lists_only_fully_configured_vendors(self) -> None:
        settings = _settings(**CENTRAL, oneview_ip="10.0.0.2", oneview_username="u")
        assert configured_manager_types(settings) == [ManagerType.UCS_CENTRAL]

    def test_empty_when_nothing_is_configured(self) -> None:
        assert configured_manager_types(_settings()) == []

    def test_ucs_manager_can_never_be_listed(self) -> None:
        """A deliberate guarantee, not an accident of which values are set:
        it has no address to be pointed at, so it is collected only as part
        of a UCS Central run.
        """
        settings = _settings(
            **CENTRAL, ucs_manager_username="domain-admin", ucs_manager_password="domain-secret"
        )
        assert ManagerType.UCS_MANAGER not in configured_manager_types(settings)


def test_connection_repr_never_shows_the_password() -> None:
    """A `ManagerConnection` ends up in tracebacks and debugger frames;
    its repr must not carry the secret there.
    """
    connection = EnvConnectionResolver(_settings(**CENTRAL)).resolve(ManagerType.UCS_CENTRAL)
    assert "s3cret" not in repr(connection)
    assert "***" in repr(connection)
