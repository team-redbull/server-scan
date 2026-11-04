"""Running a CronJob by hand and reading its Jobs, admin only (ADR-0055)."""

from __future__ import annotations

from app.application.services.audit_service import AuditService
from app.domain.models.audit_event import Actor, EventType
from app.domain.ports.job_runner import CronJobInfo, JobInfo, JobRunner


class JobTriggerService:
    """Starts a Job through the runner and records who did it."""

    def __init__(self, runner: JobRunner, audit: AuditService, *, log_tail_max: int) -> None:
        """
        Build the service.

        Args:
            runner (JobRunner): Kubernetes or the fake.
            audit (AuditService): Records each trigger.
            log_tail_max (int): The most log lines one request may ask for.
        """
        self._runner = runner
        self._audit = audit
        self._log_tail_max = log_tail_max

    async def list_cronjobs(self) -> list[CronJobInfo]:
        """
        The triggerable CronJobs with their recent Jobs.

        Returns:
            list[CronJobInfo]: Sorted by name.
        """
        return await self._runner.list_cronjobs()

    async def run(self, actor: Actor, cronjob: str) -> JobInfo:
        """
        Create the Job, then write a `JOB_TRIGGERED` event.

        Args:
            actor (Actor): The admin who clicked.
            cronjob (str): The CronJob name.

        Returns:
            JobInfo: The new Job.
        """
        job = await self._runner.run(cronjob, triggered_by=actor.id)
        # After the create so the event names the Job (the Job carries the actor too).
        await self._audit.record(
            EventType.JOB_TRIGGERED,
            actor=actor,
            data={"cronjob": cronjob, "job": job.name, "namespace": self._runner.namespace},
        )
        return job

    async def pod_logs(self, cronjob: str, job: str, pod: str, tail: int) -> str:
        """
        A pod's log tail, capped to the configured maximum.

        Args:
            cronjob (str): The CronJob name.
            job (str): A Job of that CronJob.
            pod (str): A pod of that Job.
            tail (int): Requested trailing lines.

        Returns:
            str: The log text.
        """
        return await self._runner.pod_logs(
            cronjob, job, pod, tail_lines=max(1, min(tail, self._log_tail_max))
        )
