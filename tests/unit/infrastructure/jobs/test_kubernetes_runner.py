"""`KubernetesJobRunner` against a mocked API server (ADR-0055)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.errors import ConflictError, NotFoundError
from app.infrastructure.jobs.kubernetes import KubernetesJobRunner

NS = "ns"
CJ = "rel-collector-oneview"


def _cronjob(name: str = CJ, *, labelled: bool = True) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "uid": "u-1",
            "labels": {"server-scan.io/triggerable": "true"} if labelled else {},
        },
        "spec": {
            "schedule": "0 6 * * *",
            "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}},
        },
    }


def _job(name: str, *, active: int = 0, uid: str = "u-1", pending: bool = False) -> dict[str, Any]:
    done = [] if active or pending else [{"type": "Complete", "status": "True"}]
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [{"kind": "CronJob", "name": CJ, "uid": uid}],
        },
        "status": {"active": active, "conditions": done},
    }


def _pod(name: str, job: str) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "ownerReferences": [{"kind": "Job", "name": job}]},
        "status": {"phase": "Running"},
    }


class _Api:
    """A tiny fake API server recording the calls it gets."""

    def __init__(self, jobs: list[dict[str, Any]], pods: list[dict[str, Any]]) -> None:
        self.jobs, self.pods = jobs, pods
        self.calls: list[tuple[str, str]] = []
        self.created: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        assert request.headers["Authorization"] == "Bearer tok"
        if path == f"/apis/batch/v1/namespaces/{NS}/cronjobs/{CJ}":
            return httpx.Response(200, json=_cronjob())
        if path.startswith(f"/apis/batch/v1/namespaces/{NS}/cronjobs/"):
            name = path.rsplit("/", 1)[1]
            return (
                httpx.Response(200, json=_cronjob(name, labelled=False))
                if name == "unlabelled"
                else httpx.Response(404, json={})
            )
        if path == f"/apis/batch/v1/namespaces/{NS}/jobs" and request.method == "POST":
            self.created = json.loads(request.content)
            return httpx.Response(201, json={**self.created, "status": {}})
        if path == f"/apis/batch/v1/namespaces/{NS}/jobs":
            return httpx.Response(200, json={"items": self.jobs})
        if "/jobs/" in path:
            found = [j for j in self.jobs if j["metadata"]["name"] == path.rsplit("/", 1)[1]]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={})
        if path.endswith("/log"):
            return httpx.Response(200, text="line1\nline2\n")
        if path == f"/api/v1/namespaces/{NS}/pods":
            return httpx.Response(200, json={"items": self.pods})
        pods = [p for p in self.pods if p["metadata"]["name"] == path.rsplit("/", 1)[1]]
        return httpx.Response(200, json=pods[0]) if pods else httpx.Response(404, json={})


def _runner(tmp_path: Path, api: _Api) -> KubernetesJobRunner:
    token = tmp_path / "token"
    token.write_text("tok\n")
    return KubernetesJobRunner(
        namespace=NS,
        base_url="https://k8s.test",
        token_path=token,
        transport=httpx.MockTransport(api),
    )


async def test_run_creates_a_job_in_its_own_namespace(tmp_path: Path) -> None:
    api = _Api([_job("old")], [])
    job = await _runner(tmp_path, api).run(CJ, triggered_by="dana")

    assert api.created is not None
    assert api.created["metadata"]["namespace"] == NS
    assert api.created["metadata"]["name"].startswith(f"{CJ}-manual-")
    assert job.manual is True
    assert job.triggered_by == "dana"


async def test_run_is_refused_while_an_owned_job_is_active(tmp_path: Path) -> None:
    api = _Api([_job("running", active=1)], [])

    with pytest.raises(ConflictError):
        await _runner(tmp_path, api).run(CJ, triggered_by="dana")

    assert api.created is None


async def test_run_is_refused_while_an_owned_job_is_still_pending(tmp_path: Path) -> None:
    api = _Api([_job("just-created", pending=True)], [])

    with pytest.raises(ConflictError):
        await _runner(tmp_path, api).run(CJ, triggered_by="dana")

    assert api.created is None


async def test_the_created_job_does_not_block_owner_deletion(tmp_path: Path) -> None:
    api = _Api([], [])
    await _runner(tmp_path, api).run(CJ, triggered_by="dana")

    assert api.created is not None
    assert "blockOwnerDeletion" not in api.created["metadata"]["ownerReferences"][0]


async def test_another_cronjobs_active_job_does_not_block(tmp_path: Path) -> None:
    api = _Api([_job("foreign", active=1, uid="someone-else")], [])

    await _runner(tmp_path, api).run(CJ, triggered_by="dana")

    assert api.created is not None


@pytest.mark.parametrize("name", ["unlabelled", "rel-nodes-status-nodes", "missing"])
async def test_a_cronjob_without_the_label_is_not_found(tmp_path: Path, name: str) -> None:
    api = _Api([], [])

    with pytest.raises(NotFoundError):
        await _runner(tmp_path, api).run(name, triggered_by="dana")

    assert api.created is None


async def test_logs_need_the_pod_to_belong_to_a_job_of_the_cronjob(tmp_path: Path) -> None:
    api = _Api([_job("j1"), _job("j2", uid="other")], [_pod("p1", "j1"), _pod("p2", "j2")])
    runner = _runner(tmp_path, api)

    assert await runner.pod_logs(CJ, "j1", "p1", tail_lines=10) == "line1\nline2\n"
    with pytest.raises(NotFoundError):
        await runner.pod_logs(CJ, "j1", "p2", tail_lines=10)
    with pytest.raises(NotFoundError):
        await runner.pod_logs(CJ, "j2", "p2", tail_lines=10)
    with pytest.raises(NotFoundError):
        await runner.pod_logs(CJ, "nope", "p1", tail_lines=10)


async def test_list_groups_jobs_and_pods_under_their_cronjob(tmp_path: Path) -> None:
    api = _Api([_job("j1", active=1)], [_pod("p1", "j1")])
    # list uses the label selector on the collection, which this fake does not filter.
    api_get = api.__call__

    def with_list(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cronjobs"):
            return httpx.Response(200, json={"items": [_cronjob()]})
        return api_get(request)

    token = tmp_path / "token"
    token.write_text("tok")
    runner = KubernetesJobRunner(
        namespace=NS,
        base_url="https://k8s.test",
        token_path=token,
        transport=httpx.MockTransport(with_list),
    )
    (cj,) = await runner.list_cronjobs()

    assert cj.active is True
    assert [p.name for p in cj.jobs[0].pods] == ["p1"]
