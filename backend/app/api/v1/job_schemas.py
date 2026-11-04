"""Response models of `/api/v1/jobs` (ADR-0055)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class PodOut(BaseModel):
    """One pod of a Job."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    phase: str
    started_at: str | None


class JobOut(BaseModel):
    """One Job of a CronJob."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    active: int
    succeeded: int
    failed: int
    state: Literal["active", "succeeded", "failed", "pending"]
    manual: bool
    triggered_by: str | None
    pods: list[PodOut]


class CronJobOut(BaseModel):
    """One triggerable CronJob with its recent Jobs."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    schedule: str
    suspend: bool
    last_schedule_time: str | None
    last_successful_time: str | None
    warning: str | None
    active: bool
    jobs: list[JobOut]
    enabled: bool
    deployed: bool


class JobsOverview(BaseModel):
    """`GET /jobs`: whether the feature is on here, and the CronJobs."""

    enabled: bool
    backend: Literal["kubernetes", "fake", "off"]
    namespace: str | None
    cronjobs: list[CronJobOut]


class PodLogs(BaseModel):
    """`GET .../logs`: the tail of one pod's log."""

    pod: str
    tail: int
    text: str
