"""`JobRunner` over the in-cluster Kubernetes API with plain httpx (ADR-0055)."""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.domain.ports.job_runner import TRIGGER_LABEL, CronJobInfo, JobInfo
from app.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.infrastructure.jobs.manifest import (
    build_job,
    cronjob_info,
    job_info,
    manual_job_name,
    owned_by,
)

logger = structlog.get_logger(__name__)

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_MAX_LOG_BYTES = 1_000_000


def read_namespace(path: Path = SERVICE_ACCOUNT_DIR / "namespace") -> str:
    """
    The namespace this pod runs in, from the ServiceAccount volume.

    Args:
        path (Path): The namespace file.

    Returns:
        str: The namespace.

    Raises:
        ServiceUnavailableError: The file is missing (not in a cluster).
    """
    try:
        return path.read_text().strip()
    except OSError as exc:
        raise ServiceUnavailableError(
            "Not running in a cluster: no ServiceAccount namespace."
        ) from exc


def _owned_by_cronjob(jobs: list[dict[str, Any]], cronjob: dict[str, Any]) -> list[dict[str, Any]]:
    """
    The Jobs whose ownerReference names this CronJob and its uid.

    Args:
        jobs (list[dict[str, Any]]): Job objects of the namespace.
        cronjob (dict[str, Any]): The CronJob object.

    Returns:
        list[dict[str, Any]]: The Jobs it owns.
    """
    meta = cronjob["metadata"]
    return [
        j
        for j in jobs
        if owned_by(j, "CronJob", meta["name"])
        and any(r.get("uid") == meta["uid"] for r in j["metadata"].get("ownerReferences") or [])
    ]


class KubernetesJobRunner:
    """Talks to `https://$KUBERNETES_SERVICE_HOST` as the pod's ServiceAccount, in one namespace."""

    def __init__(
        self,
        *,
        namespace: str,
        base_url: str,
        token_path: Path = SERVICE_ACCOUNT_DIR / "token",
        ca_path: Path | str = SERVICE_ACCOUNT_DIR / "ca.crt",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Build the runner.

        Args:
            namespace (str): The only namespace it reads or writes.
            base_url (str): The API server URL.
            token_path (Path): The ServiceAccount token, re-read per request (it rotates).
            ca_path (Path | str): The cluster CA bundle.
            transport (httpx.AsyncBaseTransport | None): A test transport; skips the CA.
        """
        self._namespace = namespace
        self._token_path = token_path
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=_TIMEOUT,
            transport=transport,
            verify=ssl.create_default_context(cafile=str(ca_path)) if transport is None else True,
        )

    @classmethod
    def in_cluster(cls, namespace: str | None) -> KubernetesJobRunner:
        """
        Build from the pod's own environment.

        Args:
            namespace (str | None): An explicit namespace, else the ServiceAccount's.

        Returns:
            KubernetesJobRunner: A runner for `KUBERNETES_SERVICE_HOST`.

        Raises:
            ServiceUnavailableError: The pod is not in a cluster.
        """
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise ServiceUnavailableError(
                "Not running in a cluster: KUBERNETES_SERVICE_HOST unset."
            )
        netloc = f"[{host}]" if ":" in host else host
        return cls(namespace=namespace or read_namespace(), base_url=f"https://{netloc}:{port}")

    @property
    def backend(self) -> str:
        """The backend's name."""
        return "kubernetes"

    @property
    def namespace(self) -> str:
        """The namespace it acts in."""
        return self._namespace

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        ok_404: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        One authorised request, with Kubernetes failures turned into app errors.

        Args:
            method (str): HTTP method.
            path (str): API path.
            ok_404 (bool): Return a 404 instead of raising it.
            **kwargs (Any): Passed to httpx (`params`, `json`).

        Returns:
            httpx.Response: A 2xx (or an allowed 404) response.

        Raises:
            NotFoundError: A 404 that is not allowed.
            ConflictError: Kubernetes answered 409 (a name clash).
            ServiceUnavailableError: Any other failure, or the API unreachable.
        """
        try:
            token = self._token_path.read_text().strip()
            resp = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        except (OSError, httpx.HTTPError) as exc:
            logger.warning("jobs.k8s_unreachable", error=type(exc).__name__)
            raise ServiceUnavailableError("The Kubernetes API is unreachable.") from exc
        if resp.status_code == 404:
            if ok_404:
                return resp
            raise NotFoundError("Not found in the Kubernetes API.")
        if resp.status_code == 409:
            raise ConflictError("Kubernetes refused: that Job name already exists.")
        if resp.status_code >= 400:
            logger.warning(
                "jobs.k8s_error", status=resp.status_code, path=path, body=resp.text[:300]
            )
            raise ServiceUnavailableError(f"The Kubernetes API answered {resp.status_code}.")
        return resp

    def _batch(self, kind: str) -> str:
        """
        The collection path of a batch/v1 kind in this namespace.

        Args:
            kind (str): `cronjobs` or `jobs`.

        Returns:
            str: The path.
        """
        return f"/apis/batch/v1/namespaces/{self._namespace}/{kind}"

    async def _triggerable(self, name: str) -> dict[str, Any]:
        """
        Fetch a CronJob and require the trigger label.

        Args:
            name (str): The CronJob name.

        Returns:
            dict[str, Any]: The CronJob object.

        Raises:
            NotFoundError: It does not exist or lacks the label.
        """
        resp = await self._call("GET", f"{self._batch('cronjobs')}/{name}", ok_404=True)
        body: dict[str, Any] = resp.json() if resp.status_code == 200 else {}
        labels = (body.get("metadata") or {}).get("labels") or {}
        if labels.get(TRIGGER_LABEL) != "true":
            raise NotFoundError(f"No triggerable CronJob named {name!r}.")
        return body

    async def _owned_jobs(self, cronjob: dict[str, Any]) -> list[dict[str, Any]]:
        """
        The Jobs a CronJob owns, by ownerReference name and uid.

        Args:
            cronjob (dict[str, Any]): The CronJob object.

        Returns:
            list[dict[str, Any]]: The Job objects.
        """
        items = (await self._call("GET", self._batch("jobs"))).json().get("items") or []
        return _owned_by_cronjob(items, cronjob)

    async def _pods_of(self, jobs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """
        Group the namespace's Job pods by owning Job name.

        Args:
            jobs (list[dict[str, Any]]): The Jobs of interest.

        Returns:
            dict[str, list[dict[str, Any]]]: Job name to its pods.
        """
        names = {j["metadata"]["name"] for j in jobs}
        if not names:
            return {}
        path = f"/api/v1/namespaces/{self._namespace}/pods"
        items = (await self._call("GET", path, params={"labelSelector": "job-name"})).json()
        grouped: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
        for pod in items.get("items") or []:
            for name in names:
                if owned_by(pod, "Job", name):
                    grouped[name].append(pod)
        return grouped

    async def list_cronjobs(self) -> list[CronJobInfo]:
        """
        Every labelled CronJob with its owned Jobs and their pods.

        Returns:
            list[CronJobInfo]: Sorted by name.
        """
        params = {"labelSelector": f"{TRIGGER_LABEL}=true"}
        cronjobs = (await self._call("GET", self._batch("cronjobs"), params=params)).json()
        all_jobs = (await self._call("GET", self._batch("jobs"))).json().get("items") or []
        pods = await self._pods_of(all_jobs)
        out: list[CronJobInfo] = []
        for cj in cronjobs.get("items") or []:
            meta = cj["metadata"]
            mine = _owned_by_cronjob(all_jobs, cj)
            out.append(
                cronjob_info(
                    {**cj, "metadata": {**meta, "namespace": self._namespace}},
                    [job_info(j, pods.get(j["metadata"]["name"], [])) for j in mine],
                )
            )
        return sorted(out, key=lambda c: c.name)

    async def run(self, cronjob: str, *, triggered_by: str) -> JobInfo:
        """
        Create a Job from the CronJob's template unless one of its Jobs is running.

        Args:
            cronjob (str): The CronJob name.
            triggered_by (str): The actor id, written onto the Job.

        Returns:
            JobInfo: The new Job.

        Raises:
            ConflictError: An owned Job already has a running pod.
        """
        obj = await self._triggerable(cronjob)
        for job in await self._owned_jobs(obj):
            if job_info(job, []).state in ("active", "pending"):
                raise ConflictError(
                    f"{cronjob} already has an unfinished Job ({job['metadata']['name']})."
                )
        body = build_job(
            {**obj, "metadata": {**obj["metadata"], "namespace": self._namespace}},
            name=manual_job_name(cronjob),
            triggered_by=triggered_by,
        )
        created = (await self._call("POST", self._batch("jobs"), json=body)).json()
        logger.info(
            "jobs.created", cronjob=cronjob, job=created["metadata"]["name"], by=triggered_by
        )
        return job_info(created, [])

    async def pod_logs(self, cronjob: str, job: str, pod: str, *, tail_lines: int) -> str:
        """
        A pod's log tail, after proving CronJob, Job and pod belong together.

        Args:
            cronjob (str): The CronJob name.
            job (str): A Job owned by that CronJob.
            pod (str): A pod owned by that Job.
            tail_lines (int): How many trailing lines.

        Returns:
            str: The log text.

        Raises:
            NotFoundError: A link of the ownership chain is missing.
        """
        obj = await self._triggerable(cronjob)
        job_obj = await self._call("GET", f"{self._batch('jobs')}/{job}", ok_404=True)
        if job_obj.status_code != 200 or not any(
            r.get("uid") == obj["metadata"]["uid"] and r.get("kind") == "CronJob"
            for r in job_obj.json()["metadata"].get("ownerReferences") or []
        ):
            raise NotFoundError(f"{job!r} is not a Job of {cronjob!r}.")
        base = f"/api/v1/namespaces/{self._namespace}/pods/{pod}"
        pod_resp = await self._call("GET", base, ok_404=True)
        if pod_resp.status_code != 200 or not owned_by(pod_resp.json(), "Job", job):
            raise NotFoundError(f"{pod!r} is not a pod of {job!r}.")
        resp = await self._call(
            "GET",
            f"{base}/log",
            ok_404=True,
            params={"tailLines": tail_lines, "limitBytes": _MAX_LOG_BYTES},
        )
        return resp.text if resp.status_code == 200 else ""
