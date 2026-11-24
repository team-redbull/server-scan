"""The in-memory runner: believable data and the one-running-job rule (ADR-0055)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.errors import ConflictError, NotFoundError
from app.infrastructure.jobs.fake import RUN_SECONDS, FakeJobRunner


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def test_nine_cronjobs_with_history_and_warnings() -> None:
    cronjobs = await FakeJobRunner().list_cronjobs()

    assert len(cronjobs) == 9
    warned = {c.name.removeprefix("server-scan-") for c in cronjobs if c.warning}
    assert warned == {"collector-openmanage", "prune-events", "prune-servers"}
    assert all(c.jobs and not c.active for c in cronjobs if c.deployed)


async def test_the_two_collectors_shipped_off_are_switched_off_and_cannot_be_run() -> None:
    runner = FakeJobRunner()
    off = [c for c in await runner.list_cronjobs() if not c.deployed]

    assert {c.name for c in off} == {
        "server-scan-collector-redfish-standalone",
        "server-scan-collector-kubevirt",
    }
    for cronjob in off:
        assert cronjob.enabled is False
        assert cronjob.jobs == ()
        with pytest.raises(NotFoundError):
            await runner.run(cronjob.name, triggered_by="dana")


async def test_a_manual_run_is_active_then_succeeds_and_blocks_a_second() -> None:
    clock = _Clock()
    runner = FakeJobRunner(clock)
    name = "server-scan-collector-oneview"

    job = await runner.run(name, triggered_by="dana")
    assert job.state == "active"
    with pytest.raises(ConflictError):
        await runner.run(name, triggered_by="dana")
    pod = job.pods[0].name
    assert "collector.start" in await runner.pod_logs(name, job.name, pod, tail_lines=500)

    clock.now += timedelta(seconds=RUN_SECONDS + 1)
    (cj,) = [c for c in await runner.list_cronjobs() if c.name == name]
    assert cj.jobs[0].state == "succeeded"
    assert "collector.done" in await runner.pod_logs(name, job.name, pod, tail_lines=500)
    await runner.run(name, triggered_by="dana")


async def test_unknown_names_and_foreign_pods_are_not_found() -> None:
    runner = FakeJobRunner()
    with pytest.raises(NotFoundError):
        await runner.run("rel-nodes-status-nodes", triggered_by="dana")
    with pytest.raises(NotFoundError):
        await runner.pod_logs("server-scan-prune-events", "j", "p", tail_lines=5)


async def test_one_seeded_job_was_retried_after_a_failed_first_pod() -> None:
    cronjobs = await FakeJobRunner().list_cronjobs()
    retried = [j for c in cronjobs for j in c.jobs if len(j.pods) == 2]

    assert len(retried) == 1
    job = retried[0]
    first, second = job.pods
    assert (first.phase, second.phase) == ("Failed", "Succeeded")
    assert first.started_at and first.finished_at
    assert second.started_at and second.finished_at
    assert first.started_at < first.finished_at <= second.started_at < second.finished_at
    assert job.state == "succeeded"
    assert job.finished_at == second.finished_at


async def test_every_finished_pod_has_a_finish_time_and_a_running_one_has_none() -> None:
    clock = _Clock()
    runner = FakeJobRunner(clock)
    seeded = [j for c in await runner.list_cronjobs() for j in c.jobs]
    assert all(p.finished_at for j in seeded for p in j.pods)

    running = await runner.run("server-scan-collector-oneview", triggered_by="dana")
    assert [p.finished_at for p in running.pods] == [None]


async def test_the_retried_jobs_failed_pod_has_logs_too() -> None:
    runner = FakeJobRunner()
    for cronjob in await runner.list_cronjobs():
        for job in cronjob.jobs:
            if len(job.pods) == 2:
                text = await runner.pod_logs(
                    cronjob.name, job.name, job.pods[0].name, tail_lines=50
                )
                assert "ERROR" in text
                return
    raise AssertionError("no retried job was seeded")


async def test_no_two_seeded_jobs_or_pods_share_a_name() -> None:
    for cronjob in await FakeJobRunner().list_cronjobs():
        names = [j.name for j in cronjob.jobs]
        pods = [p.name for j in cronjob.jobs for p in j.pods]
        assert len(names) == len(set(names)), cronjob.name
        assert len(pods) == len(set(pods)), cronjob.name
