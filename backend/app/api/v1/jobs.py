"""`/api/v1/jobs` — run a CronJob by hand and read its Jobs and logs, admin only (ADR-0055)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.job_schemas import CronJobOut, JobOut, JobsOverview, PodLogs
from app.application.services.audit_service import AuditService
from app.application.services.job_trigger import JobTriggerService
from app.config.jobs import JobsSettings, get_jobs_settings
from app.dependencies import get_mongo_holder, require_admin
from app.domain.models.audit_event import Actor
from app.domain.ports.job_runner import JobRunner
from app.errors import NotFoundError
from app.infrastructure.jobs.catalog import apply_catalog, load_catalog
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.client import MongoClientHolder

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"], dependencies=[Depends(require_admin)])

DEFAULT_TAIL = 500


async def get_job_runner(request: Request) -> JobRunner | None:
    """
    The runner the lifespan built, or `None` when the feature is off.

    Args:
        request (Request): The current request.

    Returns:
        JobRunner | None: `app.state.job_runner`.
    """
    runner: JobRunner | None = getattr(request.app.state, "job_runner", None)
    return runner


async def _service(
    runner: Annotated[JobRunner | None, Depends(get_job_runner)],
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[JobsSettings, Depends(get_jobs_settings)],
) -> JobTriggerService:
    """
    Build the service for one request.

    Args:
        runner (JobRunner | None): The runner.
        mongo (MongoClientHolder): Backs the audit trail.
        settings (JobsSettings): Supplies the log cap.

    Returns:
        JobTriggerService: A service bound to them.

    Raises:
        NotFoundError: The feature is off on this deployment.
    """
    if runner is None:
        raise NotFoundError("Running jobs from the UI is not enabled on this deployment.")
    audit = AuditService(repo=MongoAuditEventRepository(mongo))
    return JobTriggerService(runner, audit, log_tail_max=settings.log_tail_max)


@router.get("", response_model=JobsOverview)
async def list_jobs(
    runner: Annotated[JobRunner | None, Depends(get_job_runner)],
    settings: Annotated[JobsSettings, Depends(get_jobs_settings)],
) -> JobsOverview:
    """
    The triggerable CronJobs with their recent Jobs and pods.

    Args:
        runner (JobRunner | None): The runner, `None` when the feature is off.
        settings (JobsSettings): Names the chart's job catalog file.

    Returns:
        JobsOverview: `enabled: false` and no CronJobs when the feature is off.
    """
    if runner is None:
        return JobsOverview(enabled=False, backend="off", namespace=None, cronjobs=[])
    cronjobs = apply_catalog(await runner.list_cronjobs(), load_catalog(settings.catalog_file))
    return JobsOverview(
        enabled=True,
        backend=runner.backend,  # ty: ignore[invalid-argument-type]
        namespace=runner.namespace,
        cronjobs=[CronJobOut.model_validate(c) for c in cronjobs],
    )


@router.post("/{cronjob}/run", response_model=JobOut, status_code=201)
async def run_job(
    cronjob: str,
    actor: Annotated[Actor, Depends(require_admin)],
    service: Annotated[JobTriggerService, Depends(_service)],
) -> JobOut:
    """
    Create a Job from the CronJob, like `kubectl create job --from=cronjob/<name>`.

    Args:
        cronjob (str): The CronJob name.
        actor (Actor): The admin caller.
        service (JobTriggerService): Creates and records it.

    Returns:
        JobOut: The new Job (409 when one is already running, 404 when not triggerable).
    """
    return JobOut.model_validate(await service.run(actor, cronjob))


@router.get("/{cronjob}/runs/{job}/pods/{pod}/logs", response_model=PodLogs)
async def pod_logs(
    cronjob: str,
    job: str,
    pod: str,
    service: Annotated[JobTriggerService, Depends(_service)],
    tail: Annotated[int, Query(ge=1, le=100_000, description="Trailing lines.")] = DEFAULT_TAIL,
) -> PodLogs:
    """
    The last lines of a pod's log, only for a pod of a Job of a triggerable CronJob.

    Args:
        cronjob (str): The CronJob name.
        job (str): A Job it owns.
        pod (str): A pod that Job owns.
        service (JobTriggerService): Reads the log.
        tail (int): Trailing lines, capped by `INVENTORY_JOBS_LOG_TAIL_MAX`.

    Returns:
        PodLogs: The text.
    """
    text = await service.pod_logs(cronjob, job, pod, tail)
    return PodLogs(pod=pod, tail=tail, text=text)
