"""Centralized application configuration.

All environment lookups go through this module — never scatter `os.environ`
calls through the codebase. Settings are loaded once at startup and injected
via FastAPI dependencies (see `app.main`), not imported as a bare module-level
singleton, so tests can override them cleanly with `dependency_overrides`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_INSECURE_DEV_CURSOR_SECRET = "dev-insecure-cursor-secret-change-in-production"  # noqa: S105 - dev default, not a real secret


class Settings(BaseSettings):
    """
    Application settings, sourced from environment variables and an optional `.env` file.

    Every field's environment variable is prefixed `INVENTORY_`; see
    `.env.example` for the full list with explanations.
    """

    model_config = SettingsConfigDict(
        env_prefix="INVENTORY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "server-scan-api"
    environment: Literal["development", "test", "staging", "production"] = "development"

    # Bound inside the container, where 127.0.0.1 would be unreachable from
    # the pod network; exposure is the Service/network policy's job.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080
    # NoDecode: pydantic-settings would otherwise JSON-decode a list-typed
    # env var before `_split_csv` sees the comma-separated string.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "server-scan"
    mongo_connect_timeout_ms: int = 5_000
    mongo_server_selection_timeout_ms: int = 5_000
    mongo_socket_timeout_ms: int = 10_000
    mongo_max_pool_size: int = 100
    mongo_min_pool_size: int = 0

    redis_uri: str = "redis://localhost:6379/0"
    redis_connect_timeout_seconds: float = 2.0
    redis_socket_timeout_seconds: float = 2.0
    redis_max_connections: int = 50
    cache_default_ttl_seconds: int = 300

    default_page_size: int = 50
    max_page_size: int = 200
    cursor_secret: str = _INSECURE_DEV_CURSOR_SECRET

    max_available_count: int = 20

    capacity_aliases: str = ""

    sites: str = ""

    # The field name must equal the env var suffix — deploy/README.md,
    # "Configuration notes".
    gpu_models: str = ""

    nic_os_names: str = ""

    regex_max_pattern_length: int = 200
    regex_match_timeout_seconds: float = 0.25

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    metrics_enabled: bool = True
    stale_after_seconds: int = 43200
    metrics_fleet_refresh_seconds: float = 30.0

    # --- Collectors (tools/run_collector.py, not the API process) ---
    ucs_manager_username: str = ""
    ucs_manager_password: SecretStr = SecretStr("")

    ucs_central_ip: str = ""
    ucs_central_username: str = ""
    ucs_central_password: SecretStr = SecretStr("")

    oneview_ip: str = ""
    oneview_username: str = ""
    oneview_password: SecretStr = SecretStr("")

    oneview_api_version: int = 0

    oneview_verify_tls: bool = False

    oneview_page_size: int = 256

    oneview_collect_psus: bool = True

    oneview_psu_concurrency: int = 8

    oneview_collect_cpu_threads: bool = True
    oneview_cpu_threads_concurrency: int = 8

    ome_ip: str = ""
    ome_username: str = ""
    ome_password: SecretStr = SecretStr("")

    ome_bmc_username: str = ""
    ome_bmc_password: SecretStr = SecretStr("")
    ome_bmc_port: int = 443

    ome_bmc_verify_tls: bool = False

    intersight_ip: str = ""
    intersight_api_key_id: str = ""
    intersight_api_key_pem: SecretStr = SecretStr("")

    intersight_management_modes: str = "Intersight,IntersightStandalone"

    intersight_page_size: int = 1000

    intersight_read_timeout_seconds: float = 60.0

    intersight_run_budget_seconds: float = 1800.0

    collector_connect_timeout_seconds: float = 15.0

    redfish_username: str = ""
    redfish_password: SecretStr = SecretStr("")

    redfish_inventory_file: str = ""
    redfish_credentials_file: str = ""

    redfish_ca_bundle: str = ""
    redfish_tls_min_version: Literal["TLSv1", "TLSv1_1", "TLSv1_2", "TLSv1_3"] = "TLSv1_2"

    redfish_connect_timeout_seconds: float = 10.0
    redfish_read_timeout_seconds: float = 30.0

    redfish_host_budget_seconds: float = 180.0
    redfish_run_budget_seconds: float = 3600.0

    redfish_fleet_concurrency: int = 16

    redfish_pcie_gpu_detection: bool = False
    redfish_pcie_gpu_max_devices: int = 50
    redfish_pcie_gpu_max_gpus: int = 16
    redfish_pcie_gpu_models: str = ""

    ucs_central_domain_concurrency: int = 4

    collector_name_pattern: str = ""

    # `None` inherits `collector_name_pattern`; an explicit "" opts out of it.
    ucs_central_name_pattern: str | None = None
    intersight_name_pattern: str | None = None
    ome_name_pattern: str | None = None
    oneview_name_pattern: str | None = None
    redfish_name_pattern: str | None = None

    openshift_cluster_name: str = ""

    openshift_mce_name: str = ""

    openshift_exclude_name_parts: str = "infra,control-plane"

    openshift_request_timeout_seconds: float = 30.0

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _refuse_the_dev_cursor_secret_in_production(self) -> Settings:
        """
        Refuse the committed dev cursor secret, or a blank one, in production.

        Why this fails startup rather than warning: deploy/README.md,
        "Configuration notes".

        Returns:
            Settings: `self`, unchanged, once the check passes.

        Raises:
            ValueError: If `environment` is `"production"` and
                `cursor_secret` is still the committed insecure default,
                or blank.
        """
        if self.environment != "production":
            return self
        if not self.cursor_secret.strip():
            # An empty `secretKeyRef` value still counts as "set" to
            # pydantic-settings — a different mistake from never setting
            # it, and one that must fail the same way.
            raise ValueError(
                "INVENTORY_CURSOR_SECRET is blank with INVENTORY_ENVIRONMENT=production. "
                "Set INVENTORY_CURSOR_SECRET to a real, deployment-specific secret — "
                "see deploy/helm/server-scan's cursorSecret value."
            )
        if self.cursor_secret == _INSECURE_DEV_CURSOR_SECRET:
            raise ValueError(
                "INVENTORY_CURSOR_SECRET is still the committed dev default "
                f"({_INSECURE_DEV_CURSOR_SECRET!r}) with INVENTORY_ENVIRONMENT=production. "
                "Set INVENTORY_CURSOR_SECRET to a real, deployment-specific secret — "
                "see deploy/helm/server-scan's cursorSecret value."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """
    Return the cached `Settings` singleton, constructing it on first call.

    Tests can call `get_settings.cache_clear()` after
    `monkeypatch.setenv(...)` to pick up overrides.

    Returns:
        Settings: The process-wide settings instance.
    """
    return Settings()
