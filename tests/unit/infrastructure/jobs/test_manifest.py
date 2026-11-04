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
