"""Settings of the CronJob trigger (ADR-0055), kept apart from the main `Settings`."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobsSettings(BaseSettings):
    """
    Where the "Run job" feature gets its Jobs from; every variable is `INVENTORY_JOBS_*`.

    Attributes:
        backend (str | None): `kubernetes`, `fake` or `off`; unset means `fake` outside
            production and `off` in production (the chart sets `kubernetes` explicitly).
        namespace (str | None): The namespace to act in (the chart passes the pod's own through
            the downward API); unset reads the ServiceAccount's namespace file.
        log_tail_max (int): The most log lines one request may ask for.
        catalog_file (str | None): The chart's JSON list of every triggerable job and whether
            `values.yaml` enables it; unset means every listed CronJob counts as enabled.
    """

    model_config = SettingsConfigDict(
        env_prefix="INVENTORY_JOBS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    backend: Literal["kubernetes", "fake", "off"] | None = None
    namespace: str | None = None
    log_tail_max: int = Field(default=2000, ge=1)
    catalog_file: str | None = None


@lru_cache
def get_jobs_settings() -> JobsSettings:
    """
    The process-wide jobs settings.

    Returns:
        JobsSettings: Loaded once from the environment.
    """
    return JobsSettings()


def effective_backend(
    settings: JobsSettings, environment: str
) -> Literal["kubernetes", "fake", "off"]:
    """
    Resolve the backend, defaulting to the fake only where fake data is harmless.

    Args:
        settings (JobsSettings): The jobs settings.
        environment (str): `Settings.environment`.

    Returns:
        Literal["kubernetes", "fake", "off"]: What to build.
    """
    if settings.backend is not None:
        return settings.backend
    return "off" if environment in ("production", "staging") else "fake"
