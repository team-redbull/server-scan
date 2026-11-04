"""An in-memory `JobRunner` so dev, tests and screenshots show believable CronJobs (ADR-0055)."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.domain.ports.job_runner import CronJobInfo, JobInfo, PodInfo
from app.errors import ConflictError, NotFoundError
from app.infrastructure.jobs.manifest import manual_job_name

RUN_SECONDS = 20.0
_OPENMANAGE_WARNING = (
    "This run can restart an iDRAC (ADR-0050) when a Dell has every disk or NIC UNKNOWN."
)
_PRUNE_WARNING = "This run can permanently delete data (stale servers or audit events)."
_RELEASE = "server-scan"
_CRONJOBS: tuple[tuple[str, str, str | None], ...] = (
    (f"{_RELEASE}-collector-fake", "*/30 * * * *", None),
    (f"{_RELEASE}-collector-intersight", "15 6 * * *", None),
    (f"{_RELEASE}-collector-oneview", "30 6 * * *", None),
    (f"{_RELEASE}-collector-openmanage", "0 6 * * *", _OPENMANAGE_WARNING),
    (f"{_RELEASE}-collector-redfish-standalone", "45 6 * * *", None),
    (f"{_RELEASE}-collector-ucs-central", "0 5 * * *", None),
    (f"{_RELEASE}-prune-events", "30 3 * * 0", _PRUNE_WARNING),
    (f"{_RELEASE}-prune-servers", "0 3,9,15,21 * * *", _PRUNE_WARNING),
)


# Not deployed, as a collector with `enabled: false` in values.yaml is not.
_DISABLED = frozenset({f"{_RELEASE}-collector-redfish-standalone"})


@dataclass
class _FakeJob:
    """A fake Job: when it started and how it ends once `RUN_SECONDS` have passed."""

    name: str
    cronjob: str
    started: datetime
    ok: bool
    manual: bool
    triggered_by: str | None = None
    pod: str = ""


@dataclass
class FakeJobRunner:
    """Eight believable CronJobs, a short history each, and a trigger that runs for 20 seconds."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _jobs: dict[str, list[_FakeJob]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Seed a succeeded Job per CronJob, and a failed one for the Intersight collector."""
        now = self.clock()
        for i, (name, _, _) in enumerate(_CRONJOBS):
            history = [
                self._past(name, now - timedelta(hours=26 + i), ok=True),
                self._past(name, now - timedelta(hours=2 + i), ok=True),
            ]
            if name.endswith("intersight"):
                history.append(self._past(name, now - timedelta(hours=14), ok=False))
            self._jobs[name] = history

    def _past(self, cronjob: str, started: datetime, *, ok: bool) -> _FakeJob:
        """
        A finished scheduled Job.

        Args:
            cronjob (str): Its CronJob.
            started (datetime): When it started.
            ok (bool): Whether it succeeded.

        Returns:
            _FakeJob: The Job.
        """
        stamp = int(started.timestamp()) // 60
        name = f"{cronjob}-{stamp}"
        return _FakeJob(name, cronjob, started, ok, False, pod=f"{name}-x7k2p")

    @property
    def backend(self) -> str:
        """The backend's name."""
        return "fake"

    @property
    def namespace(self) -> str:
        """The fake namespace."""
        return "server-scan"

    def _info(self, job: _FakeJob) -> JobInfo:
        """
        Render a fake Job as the port type, by how long it has run.

        Args:
            job (_FakeJob): The Job.

        Returns:
            JobInfo: Active until `RUN_SECONDS`, then succeeded or failed.
        """
        now = self.clock()
        started = job.started
        running = job.manual and (now - started).total_seconds() < RUN_SECONDS
        state = "active" if running else ("succeeded" if job.ok else "failed")
        stamp = started.isoformat()
        end = None if running else (started + timedelta(seconds=RUN_SECONDS)).isoformat()
        return JobInfo(
            name=job.name,
            created_at=stamp,
            started_at=stamp,
            finished_at=end,
            active=1 if running else 0,
            succeeded=1 if state == "succeeded" else 0,
            failed=0 if state != "failed" else 1,
            state=state,
            manual=job.manual,
            triggered_by=job.triggered_by,
            pods=(
                PodInfo(
                    job.pod, "Running" if running else ("Succeeded" if job.ok else "Failed"), stamp
                ),
            ),
        )

    async def list_cronjobs(self) -> list[CronJobInfo]:
        """
        The eight CronJobs with their fake Jobs.

        Returns:
            list[CronJobInfo]: Sorted by name.
        """
        out = []
        for name, schedule, warning in _CRONJOBS:
            if name in _DISABLED:
                out.append(
                    CronJobInfo(
                        name=name,
                        schedule="",
                        suspend=False,
                        last_schedule_time=None,
                        last_successful_time=None,
                        warning=None,
                        active=False,
                        enabled=False,
                        deployed=False,
                    )
                )
                continue
            jobs = tuple(
                sorted(
                    (self._info(j) for j in self._jobs[name]),
                    key=lambda j: j.created_at or "",
                    reverse=True,
                )
            )
            ok = [j for j in jobs if j.state == "succeeded"]
            out.append(
                CronJobInfo(
                    name=name,
                    schedule=schedule,
                    suspend=False,
                    last_schedule_time=jobs[0].created_at if jobs else None,
                    last_successful_time=ok[0].finished_at if ok else None,
                    warning=warning,
                    active=any(j.active for j in jobs),
                    jobs=jobs,
                )
            )
        return sorted(out, key=lambda c: c.name)

    async def run(self, cronjob: str, *, triggered_by: str) -> JobInfo:
        """
        Start a fake Job that runs for 20 seconds and then succeeds.

        Args:
            cronjob (str): The CronJob name.
            triggered_by (str): The actor id.

        Returns:
            JobInfo: The new, active Job.

        Raises:
            NotFoundError: Not one of the fake CronJobs.
            ConflictError: One of its Jobs is still running.
        """
        if cronjob not in self._jobs or cronjob in _DISABLED:
            raise NotFoundError(f"No triggerable CronJob named {cronjob!r}.")
        if any(self._info(j).active for j in self._jobs[cronjob]):
            raise ConflictError(f"{cronjob} already has a running Job.")
        name = manual_job_name(cronjob)
        job = _FakeJob(
            name,
            cronjob,
            self.clock(),
            True,
            True,
            triggered_by,
            pod=f"{name}-{secrets.token_hex(2)}",
        )
        self._jobs[cronjob].append(job)
        return self._info(job)

    async def pod_logs(self, cronjob: str, job: str, pod: str, *, tail_lines: int) -> str:
        """
        Fake log lines that grow while the Job runs.

        Args:
            cronjob (str): The CronJob name.
            job (str): A fake Job of it.
            pod (str): That Job's pod.
            tail_lines (int): How many trailing lines.

        Returns:
            str: The log text.

        Raises:
            NotFoundError: The CronJob, Job or pod is not a fake one of the chain.
        """
        found = next(
            (j for j in self._jobs.get(cronjob, []) if j.name == job and j.pod == pod), None
        )
        if found is None:
            raise NotFoundError("No such Job or pod for that CronJob.")
        info = self._info(found)
        steps = [
            f"INFO collector.start source={cronjob}",
            "INFO collector.login ok",
            "INFO collector.fetch servers=2504",
            "INFO ingest.progress done=1000",
            "INFO ingest.progress done=2000",
            "INFO ingest.progress done=2504",
        ]
        if info.state == "failed":
            steps = [
                *steps[:2],
                "ERROR collector.fetch timeout after 240s",
                "ERROR collector.failed exit=1",
            ]
        elif info.state == "succeeded":
            steps.append("INFO collector.done created=0 updated=12 unchanged=2492 errors=0")
        elif found.manual:
            elapsed = (self.clock() - found.started).total_seconds()
            steps = steps[: 1 + int(elapsed / RUN_SECONDS * (len(steps) - 1))]
        return "\n".join(steps[-tail_lines:]) + "\n"
