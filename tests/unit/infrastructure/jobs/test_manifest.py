"""The Job built from a CronJob template, names, and Job state (ADR-0055)."""

from __future__ import annotations

from typing import Any

from app.infrastructure.jobs.manifest import (
    MAX_NAME,
    build_job,
    cronjob_info,
    job_info,
    manual_job_name,
)


def _cronjob() -> dict[str, Any]:
    return {
        "metadata": {"name": "rel-collector-oneview", "namespace": "ns", "uid": "u-1"},
        "spec": {
            "jobTemplate": {
                "metadata": {"labels": {"a": "b"}, "annotations": {"x": "y"}},
                "spec": {"backoffLimit": 0, "template": {"spec": {"containers": [{"name": "c"}]}}},
            }
        },
    }


def test_the_job_copies_the_template_and_is_owned_by_the_cronjob() -> None:
    job = build_job(_cronjob(), name="rel-collector-oneview-manual-abc123", triggered_by="dana")

    assert job["spec"] == _cronjob()["spec"]["jobTemplate"]["spec"]
    assert job["spec"] is not _cronjob()["spec"]["jobTemplate"]["spec"]
    assert job["metadata"]["labels"] == {"a": "b"}
    assert job["metadata"]["annotations"] == {
        "x": "y",
        "cronjob.kubernetes.io/instantiate": "manual",
        "server-scan.io/triggered-by": "dana",
    }
    (owner,) = job["metadata"]["ownerReferences"]
    assert (owner["kind"], owner["name"], owner["uid"], owner["controller"]) == (
        "CronJob",
        "rel-collector-oneview",
        "u-1",
        True,
    )


def test_a_job_name_always_fits_63_characters_and_stays_a_dns_label() -> None:
    for cronjob in ("short", "x" * 52, "y" * 60, "z" * 56 + "-"):
        name = manual_job_name(cronjob)
        assert len(name) <= MAX_NAME
        assert "-manual-" in name
        assert "--" not in name


def test_job_state_comes_from_active_and_conditions() -> None:
    def job(status: dict[str, Any]) -> str:
        return job_info({"metadata": {"name": "j"}, "status": status}, []).state

    assert job({"active": 1}) == "active"
    assert job({"conditions": [{"type": "Complete", "status": "True"}]}) == "succeeded"
    assert job({"conditions": [{"type": "Failed", "status": "True"}]}) == "failed"
    assert job({}) == "pending"


def test_a_cronjob_is_active_when_any_job_is_and_lists_newest_first() -> None:
    old = job_info({"metadata": {"name": "old", "creationTimestamp": "2026-01-01T00:00:00Z"}}, [])
    new = job_info(
        {
            "metadata": {"name": "new", "creationTimestamp": "2026-01-02T00:00:00Z"},
            "status": {"active": 1},
        },
        [],
    )
    info = cronjob_info(_cronjob(), [old, new])

    assert info.active is True
    assert [j.name for j in info.jobs] == ["new", "old"]


def _pod(name: str, created: str, started: str | None, finished: str | None, phase: str) -> dict:
    status: dict[str, Any] = {"phase": phase}
    if started:
        status["startTime"] = started
    if finished:
        status["containerStatuses"] = [
            {"name": "c", "state": {"terminated": {"finishedAt": finished}}}
        ]
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "status": status,
    }


def test_a_pod_reports_when_it_finished_from_its_terminated_container() -> None:
    info = job_info(
        {"metadata": {"name": "j"}, "status": {}},
        [
            _pod(
                "p-done",
                "2026-01-01T10:00:00Z",
                "2026-01-01T10:00:05Z",
                "2026-01-01T10:20:00Z",
                "Failed",
            ),
            _pod("p-live", "2026-01-01T10:21:00Z", "2026-01-01T10:21:05Z", None, "Running"),
        ],
    )

    assert [(p.started_at, p.finished_at) for p in info.pods] == [
        ("2026-01-01T10:00:05Z", "2026-01-01T10:20:00Z"),
        ("2026-01-01T10:21:05Z", None),
    ]


def test_a_retry_pod_keeps_its_own_times_apart_from_the_jobs() -> None:
    """backoffLimit retries run in the same Job, so the Job's own times span both attempts."""
    job = {
        "metadata": {"name": "j", "creationTimestamp": "2026-01-01T10:00:00Z"},
        "status": {
            "startTime": "2026-01-01T10:00:00Z",
            "completionTime": "2026-01-01T10:40:00Z",
            "failed": 1,
            "succeeded": 1,
            "conditions": [{"type": "Complete", "status": "True"}],
        },
    }
    info = job_info(
        job,
        [
            _pod(
                "second",
                "2026-01-01T10:20:30Z",
                "2026-01-01T10:20:35Z",
                "2026-01-01T10:40:00Z",
                "Succeeded",
            ),
            _pod(
                "first",
                "2026-01-01T10:00:00Z",
                "2026-01-01T10:00:05Z",
                "2026-01-01T10:20:00Z",
                "Failed",
            ),
        ],
    )

    assert info.state == "succeeded"
    assert [p.name for p in info.pods] == ["first", "second"]
    assert info.pods[0].finished_at == "2026-01-01T10:20:00Z"
    assert info.pods[1].started_at == "2026-01-01T10:20:35Z"


def test_a_pod_whose_init_container_failed_still_has_a_finish_time() -> None:
    pod = {
        "metadata": {"name": "p", "creationTimestamp": "2026-01-01T10:00:00Z"},
        "status": {
            "phase": "Failed",
            "startTime": "2026-01-01T10:00:05Z",
            "initContainerStatuses": [
                {"name": "init", "state": {"terminated": {"finishedAt": "2026-01-01T10:00:30Z"}}}
            ],
        },
    }

    assert job_info({"metadata": {"name": "j"}, "status": {}}, [pod]).pods[0].finished_at == (
        "2026-01-01T10:00:30Z"
    )


def test_the_pod_finish_is_its_latest_termination_across_all_containers() -> None:
    pod = {
        "metadata": {"name": "p"},
        "status": {
            "phase": "Succeeded",
            "initContainerStatuses": [
                {"name": "sidecar", "state": {"terminated": {"finishedAt": "2026-01-01T10:25:00Z"}}}
            ],
            "containerStatuses": [
                {"name": "app", "state": {"terminated": {"finishedAt": "2026-01-01T10:20:00Z"}}}
            ],
        },
    }

    assert job_info({"metadata": {"name": "j"}, "status": {}}, [pod]).pods[0].finished_at == (
        "2026-01-01T10:25:00Z"
    )


def test_pods_created_in_the_same_second_are_ordered_by_name() -> None:
    same = "2026-01-01T10:00:00Z"
    info = job_info(
        {"metadata": {"name": "j"}, "status": {}},
        [_pod("b", same, None, None, "Pending"), _pod("a", same, None, None, "Pending")],
    )

    assert [p.name for p in info.pods] == ["a", "b"]
