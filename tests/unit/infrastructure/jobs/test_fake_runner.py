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
