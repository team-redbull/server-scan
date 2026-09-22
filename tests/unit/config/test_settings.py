"""
`app.config.settings.Settings` — pinning field-name-to-env-var mapping.
Each env var name is derived from its field name (`env_prefix="INVENTORY_"`)
and `extra="ignore"` never raises on a stray one, so a misnamed field
silently reads the wrong variable; `gpu_models` did exactly that once.
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from app.config.settings import Settings

pytestmark = pytest.mark.unit


def _settings() -> Settings:
    """Build `Settings` from the process env alone, ignoring any real `.env`
    on disk; callers set what they care about via `monkeypatch` first.

    Returns:
        Settings: Constructed from the process env, `.env` excluded.
    """
    return Settings(_env_file=None)


class TestGpuModels:
    def test_inventory_gpu_models_populates_gpu_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "INVENTORY_GPU_MODELS", "P1001-200:NVIDIA A100 40GB:40,P1001-220:NVIDIA A100 80GB:80"
        )
        settings = _settings()
        assert settings.gpu_models == "P1001-200:NVIDIA A100 40GB:40,P1001-220:NVIDIA A100 80GB:80"

    def test_unset_gpu_models_is_the_documented_empty_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INVENTORY_GPU_MODELS", raising=False)
        assert _settings().gpu_models == ""


class TestSitesStillMatchesTheSameConvention:
    def test_inventory_sites_populates_sites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INVENTORY_SITES", "tlv:Tel Aviv")
        assert _settings().sites == "tlv:Tel Aviv"


class TestCursorSecretProductionFailFast:
    """`INVENTORY_CURSOR_SECRET` was previously "only a code comment, not
    enforced at startup" — an install that forgot to set it started up
    looking healthy and stayed on the committed default forever.
    """

    def test_the_dev_default_in_production_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INVENTORY_CURSOR_SECRET", raising=False)
        with pytest.raises(ValidationError, match="INVENTORY_CURSOR_SECRET"):
            Settings(_env_file=None, environment="production")

    def test_a_blank_secret_in_production_also_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty `secretKeyRef` value must fail the same way as an unset one,
        not fall through to the dev default silently.
        """
        monkeypatch.setenv("INVENTORY_CURSOR_SECRET", "")
        with pytest.raises(ValidationError, match="INVENTORY_CURSOR_SECRET"):
            Settings(_env_file=None, environment="production")

    def test_a_real_secret_in_production_starts_fine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INVENTORY_CURSOR_SECRET", "a-real-deployment-specific-secret")
        settings = Settings(_env_file=None, environment="production")
        assert settings.cursor_secret == "a-real-deployment-specific-secret"

    @pytest.mark.parametrize("environment", ["development", "test", "staging"])
    def test_the_dev_default_is_fine_outside_production(
        self,
        environment: Literal["development", "test", "staging"],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dev/test/staging must not be forced to set a real secret just
        to boot — only a `production` install signs cursors an attacker
        might ever have reason to forge.
        """
        monkeypatch.delenv("INVENTORY_CURSOR_SECRET", raising=False)
        settings = Settings(_env_file=None, environment=environment)
        assert settings.cursor_secret == "dev-insecure-cursor-secret-change-in-production"


class TestAuthEnabledRequiresAdConfig:
    """`INVENTORY_AUTH_ENABLED=true` with no AD reachable must fail at
    startup, not serve every request as a 503 (docs/adr/0034).
    """

    def test_enabled_with_no_ad_config_refuses_to_start(self) -> None:
        with pytest.raises(ValidationError, match="INVENTORY_LDAP_SERVER"):
            Settings(_env_file=None, auth_enabled=True)

    def test_disabled_needs_no_ad_config(self) -> None:
        settings = Settings(_env_file=None, auth_enabled=False)
        assert settings.ldap_server == ""

    def test_enabled_with_full_ad_config_starts_fine(self) -> None:
        settings = Settings(
            _env_file=None,
            auth_enabled=True,
            ldap_server="dc.example.com",
            ldap_domain="EXAMPLE",
            ad_api_url="https://ad-api.example.com",
            ad_api_client_id="a-real-client-id",
        )
        assert settings.auth_enabled is True


class TestSessionSecretProductionFailFast:
    def test_the_dev_default_in_production_with_auth_enabled_refuses_to_start(self) -> None:
        with pytest.raises(ValidationError, match="INVENTORY_SESSION_SECRET"):
            Settings(
                _env_file=None,
                environment="production",
                cursor_secret="a-real-deployment-specific-secret",
                auth_enabled=True,
                ldap_server="dc.example.com",
                ldap_domain="EXAMPLE",
                ad_api_url="https://ad-api.example.com",
                ad_api_client_id="a-real-client-id",
            )

    def test_the_dev_default_in_production_with_auth_disabled_is_fine(self) -> None:
        """Auth is off, so no session is ever signed — the secret is moot."""
        settings = Settings(
            _env_file=None,
            environment="production",
            cursor_secret="a-real-deployment-specific-secret",
            auth_enabled=False,
        )
        assert settings.session_secret == "dev-insecure-session-secret-change-in-production"
