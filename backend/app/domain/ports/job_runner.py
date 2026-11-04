"""The CronJob trigger seam: list CronJobs, start one, read a pod's log (ADR-0055)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

TRIGGER_LABEL = "server-scan.io/triggerable"
TRIGGER_WARNING_ANNOTATION = "server-scan.io/trigger-warning"
TRIGGERED_BY_ANNOTATION = "server-scan.io/triggered-by"


@dataclass(frozen=True, slots=True)
class PodInfo:
    """
    One pod of a Job.

    Attributes:
        name (str): The pod name.
        phase (str): Kubernetes pod phase (`Pending`, `Running`, `Succeeded`, `Failed`).
        started_at (str | None): ISO start time, `None` before the pod started.
    """

    name: str
    phase: str
    started_at: str | None = None


@dataclass(frozen=True, slots=True)
class JobInfo:
    """
    One Job owned by a triggerable CronJob.

    Attributes:
        name (str): The Job name.
        created_at (str | None): ISO creation time.
        started_at (str | None): ISO `status.startTime`.
        finished_at (str | None): ISO `status.completionTime` or failure time.
        active (int): Running pods (`status.active`).
        succeeded (int): `status.succeeded`.
        failed (int): `status.failed`.
        state (str): `active`, `succeeded`, `failed` or `pending`.
        manual (bool): Started by hand (`cronjob.kubernetes.io/instantiate: manual`).
        triggered_by (str | None): The actor id recorded on a manual Job.
        pods (tuple[PodInfo, ...]): Its pods, oldest first.
    """

    name: str
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    active: int
    succeeded: int
    failed: int
    state: str
    manual: bool
    triggered_by: str | None
    pods: tuple[PodInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class CronJobInfo:
    """
    One triggerable CronJob and its recent Jobs.

    Attributes:
        name (str): The CronJob name.
        schedule (str): Its cron expression.
        suspend (bool): `spec.suspend`.
        last_schedule_time (str | None): ISO time the schedule last fired.
        last_successful_time (str | None): ISO time a Job last succeeded.
        warning (str | None): What running it can do, shown in the confirm dialog.
        active (bool): Some owned Job has a running pod, so a manual run is refused.
        jobs (tuple[JobInfo, ...]): Recent owned Jobs, newest first.
        enabled (bool): Whether the deployment's `values.yaml` turns this job on; a CronJob the
            catalog does not know counts as enabled.
        deployed (bool): Whether the CronJob exists in the cluster (a disabled one does not).
    """

    name: str
    schedule: str
    suspend: bool
    last_schedule_time: str | None
    last_successful_time: str | None
    warning: str | None
    active: bool
    jobs: tuple[JobInfo, ...] = ()
    enabled: bool = True
    deployed: bool = True


class JobRunner(Protocol):
    """Reads and starts the Jobs of the allow-listed CronJobs, in one namespace."""

    @property
    def backend(self) -> str:
        """The backend's name, `kubernetes` or `fake`."""
        ...

    @property
    def namespace(self) -> str:
        """The namespace it acts in."""
        ...

    async def list_cronjobs(self) -> list[CronJobInfo]:
        """
        Every triggerable CronJob with its recent Jobs and their pods.

        Returns:
            list[CronJobInfo]: Sorted by name.
        """
        ...

    async def run(self, cronjob: str, *, triggered_by: str) -> JobInfo:
        """
        Create a Job from the CronJob's template, like `kubectl create job --from`.

        Args:
            cronjob (str): The CronJob name.
            triggered_by (str): The actor id, written onto the Job.

        Returns:
            JobInfo: The new Job.

        Raises:
            NotFoundError: Not a triggerable CronJob of this namespace.
            ConflictError: The CronJob already has a running Job.
        """
        ...

    async def pod_logs(self, cronjob: str, job: str, pod: str, *, tail_lines: int) -> str:
        """
        The last lines of one pod's log.

        Args:
            cronjob (str): The CronJob name.
            job (str): A Job owned by that CronJob.
            pod (str): A pod owned by that Job.
            tail_lines (int): How many trailing lines.

        Returns:
            str: The log text.

        Raises:
            NotFoundError: The CronJob is not triggerable, or the Job/pod is not its own.
        """
        ...
