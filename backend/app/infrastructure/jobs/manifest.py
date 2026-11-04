"""Pure helpers that shape a Job from a CronJob and read Kubernetes objects (ADR-0055)."""

from __future__ import annotations

import copy
import secrets
import string
from typing import Any

from app.domain.ports.job_runner import (
    TRIGGER_WARNING_ANNOTATION,
    TRIGGERED_BY_ANNOTATION,
    CronJobInfo,
    JobInfo,
    PodInfo,
)

MAX_NAME = 63
MANUAL_INFIX = "-manual-"
SUFFIX_LENGTH = 6
_ALPHABET = string.ascii_lowercase + string.digits
INSTANTIATE_ANNOTATION = "cronjob.kubernetes.io/instantiate"


def manual_job_name(cronjob: str) -> str:
    """
    A Job name `<cronjob>-manual-<suffix>` that fits the 63-character label limit.

    Args:
        cronjob (str): The CronJob name.

    Returns:
        str: The name; the CronJob part is truncated (and trimmed of a trailing dash) if needed.
    """
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(SUFFIX_LENGTH))
    room = MAX_NAME - len(MANUAL_INFIX) - SUFFIX_LENGTH
    return f"{cronjob[:room].rstrip('-')}{MANUAL_INFIX}{suffix}"


def build_job(cronjob: dict[str, Any], *, name: str, triggered_by: str) -> dict[str, Any]:
    """
    Build a Job body from a CronJob's `jobTemplate`, as `kubectl create job --from` does.

    Args:
        cronjob (dict[str, Any]): The CronJob object as the API returned it.
        name (str): The new Job's name.
        triggered_by (str): The actor id, recorded as an annotation.

    Returns:
        dict[str, Any]: The Job: template labels/annotations and spec, the CronJob as owner and
            `cronjob.kubernetes.io/instantiate: manual`.
    """
    template = cronjob["spec"]["jobTemplate"]
    meta = cronjob["metadata"]
    template_meta = template.get("metadata") or {}
    annotations = {
        **(template_meta.get("annotations") or {}),
        INSTANTIATE_ANNOTATION: "manual",
        TRIGGERED_BY_ANNOTATION: triggered_by,
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": meta["namespace"],
            "labels": dict(template_meta.get("labels") or {}),
            "annotations": annotations,
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "CronJob",
                    "name": meta["name"],
                    "uid": meta["uid"],
                    "controller": True,
                }
            ],
        },
        "spec": copy.deepcopy(template["spec"]),
    }


def owned_by(obj: dict[str, Any], kind: str, name: str) -> bool:
    """
    Whether an object has an ownerReference to one named owner.

    Args:
        obj (dict[str, Any]): A Kubernetes object.
        kind (str): The owner kind (`CronJob`, `Job`).
        name (str): The owner name.

    Returns:
        bool: True when it is listed.
    """
    refs = (obj.get("metadata") or {}).get("ownerReferences") or []
    return any(r.get("kind") == kind and r.get("name") == name for r in refs)


def _condition(status: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """
    The Job condition of one type, when it is True.

    Args:
        status (dict[str, Any]): `Job.status`.
        kind (str): `Complete` or `Failed`.

    Returns:
        dict[str, Any] | None: The condition, or `None`.
    """
    for cond in status.get("conditions") or []:
        if cond.get("type") == kind and cond.get("status") == "True":
            return cond
    return None


def job_info(job: dict[str, Any], pods: list[dict[str, Any]]) -> JobInfo:
    """
    Reduce a Job and its pods to what the UI shows.

    Args:
        job (dict[str, Any]): The Job object.
        pods (list[dict[str, Any]]): The pod objects it owns.

    Returns:
        JobInfo: Name, times, counters, a state and its pods.
    """
    meta = job.get("metadata") or {}
    status = job.get("status") or {}
    active = int(status.get("active") or 0)
    failed_cond = _condition(status, "Failed")
    done_cond = _condition(status, "Complete")
    if active:
        state = "active"
    elif failed_cond:
        state = "failed"
    elif done_cond:
        state = "succeeded"
    else:
        state = "pending"
    finished = (done_cond or failed_cond or {}).get("lastTransitionTime")
    annotations = meta.get("annotations") or {}
    ordered = sorted(pods, key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "")
    return JobInfo(
        name=meta["name"],
        created_at=meta.get("creationTimestamp"),
        started_at=status.get("startTime"),
        finished_at=status.get("completionTime") or finished,
        active=active,
        succeeded=int(status.get("succeeded") or 0),
        failed=int(status.get("failed") or 0),
        state=state,
        manual=annotations.get(INSTANTIATE_ANNOTATION) == "manual",
        triggered_by=annotations.get(TRIGGERED_BY_ANNOTATION),
        pods=tuple(
            PodInfo(
                name=p["metadata"]["name"],
                phase=(p.get("status") or {}).get("phase") or "Unknown",
                started_at=(p.get("status") or {}).get("startTime"),
            )
            for p in ordered
        ),
    )


def cronjob_info(cronjob: dict[str, Any], jobs: list[JobInfo]) -> CronJobInfo:
    """
    Reduce a CronJob and its Jobs to what the UI shows.

    Args:
        cronjob (dict[str, Any]): The CronJob object.
        jobs (list[JobInfo]): The Jobs it owns, already reduced.

    Returns:
        CronJobInfo: Its schedule, state and Jobs newest first.
    """
    meta = cronjob["metadata"]
    spec = cronjob.get("spec") or {}
    status = cronjob.get("status") or {}
    newest = sorted(jobs, key=lambda j: j.created_at or "", reverse=True)
    return CronJobInfo(
        name=meta["name"],
        schedule=spec.get("schedule", ""),
        suspend=bool(spec.get("suspend")),
        last_schedule_time=status.get("lastScheduleTime"),
        last_successful_time=status.get("lastSuccessfulTime"),
        warning=(meta.get("annotations") or {}).get(TRIGGER_WARNING_ANNOTATION),
        active=any(j.state in ("active", "pending") for j in newest),
        jobs=tuple(newest),
    )
