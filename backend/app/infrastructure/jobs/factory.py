"""Pick the `JobRunner` for the configured backend (ADR-0055)."""

from __future__ import annotations

import ssl

import structlog

from app.domain.ports.job_runner import JobRunner
from app.errors import ServiceUnavailableError
from app.infrastructure.jobs.fake import FakeJobRunner
from app.infrastructure.jobs.kubernetes import KubernetesJobRunner

logger = structlog.get_logger(__name__)


def build_job_runner(backend: str, namespace: str | None) -> JobRunner | None:
    """
    Build the runner for a backend name.

    Args:
        backend (str): `kubernetes`, `fake` or `off`.
        namespace (str | None): The namespace for `kubernetes`; `None` reads the ServiceAccount's.

    Returns:
        JobRunner | None: `None` when the feature is off.
    """
    if backend == "kubernetes":
        try:
            return KubernetesJobRunner.in_cluster(namespace)
        except (ServiceUnavailableError, OSError, ssl.SSLError) as exc:
            logger.exception("jobs.disabled", reason=str(getattr(exc, "detail", exc)))
            return None
    if backend == "fake":
        return FakeJobRunner()
    return None
