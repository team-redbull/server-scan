"""`ClusterDecommissionService`: list membership jobs, retire a destroyed cluster's (ADR-0067)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.application.services.audit_service import AuditService
from app.application.services.openshift_membership import OpenShiftMembershipService
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.openshift import MembershipRun
from app.domain.ports.repository import ServerRepository
from app.errors import NotFoundError, ValidationAppError
from app.infrastructure.mongodb.membership_run_repository import MongoMembershipRunRepository

KINDS = ("nodes", "agents")


def scope_of(kind: str, name: str) -> dict[str, object]:
    """
    The Mongo filter for the servers one membership job owns (the one the job reconciles with).

    Args:
        kind (str): `nodes` or `agents`.
        name (str): The cluster (`nodes`) or MCE (`agents`) name.

    Returns:
        dict[str, object]: The ownership filter.

    Raises:
        ValidationAppError: `kind` is neither `nodes` nor `agents`.
    """
    if kind == "nodes":
        return {"openshift.cluster_name": name}
    if kind == "agents":
        return {"openshift.mce_name": name}
    raise ValidationAppError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}.")


@dataclass(frozen=True, slots=True)
class ClusterJob:
    """One membership job, as the Clusters page lists it.

    Attributes:
        kind (str): `nodes` or `agents`.
        name (str): The cluster or MCE it reports.
        held (int): Servers that currently name it.
        last_run_at (datetime): When its newest run finished.
        silent_seconds (int): Seconds since then.
        silent (bool): Silent for longer than the threshold: presumed destroyed.
    """

    kind: str
    name: str
    held: int
    last_run_at: datetime
    silent_seconds: int
    silent: bool


class ClusterDecommissionService:
    """Lists membership jobs by how recently they ran, and retires one an operator says is gone."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        runs: MongoMembershipRunRepository,
        audit: AuditService,
        actor: Actor,
    ) -> None:
        """
        Build the service for one request.

        Args:
            server_repo (ServerRepository): The servers collection.
            runs (MongoMembershipRunRepository): The membership run records.
            audit (AuditService): Records the decommission.
            actor (Actor): The admin the freed servers' events are attributed to.
        """
        self._servers = server_repo
        self._runs = runs
        self._audit = audit
        self._actor = actor

    async def list_jobs(self, *, now: datetime, silent_after_seconds: int) -> list[ClusterJob]:
        """
        Every membership job with a run record, silent ones first.

        Args:
            now (datetime): The comparison instant.
            silent_after_seconds (int): How long without a run counts as silent.

        Returns:
            list[ClusterJob]: One per `kind`/name, longest silent first, then by name.
        """
        jobs: list[ClusterJob] = []
        for run in await self._runs.list_all():
            silent_for = max(0, int((now - run.finished_at).total_seconds()))
            jobs.append(
                ClusterJob(
                    kind=run.kind,
                    name=run.reported_by,
                    held=await self._servers.count(scope_of(run.kind, run.reported_by)),
                    last_run_at=run.finished_at,
                    silent_seconds=silent_for,
                    silent=silent_for > silent_after_seconds,
                )
            )
        return sorted(jobs, key=lambda job: (-job.silent_seconds, job.name))

    async def decommission(self, kind: str, name: str) -> tuple[MembershipRun, list[str]]:
        """
        Free what a destroyed cluster still holds and drop its run record.

        A live job re-claims its servers and rewrites its record on its next run, so retiring a
        cluster that was not gone is undone by the cluster itself.

        Args:
            kind (str): `nodes` or `agents`.
            name (str): The cluster or MCE name.

        Returns:
            tuple[MembershipRun, list[str]]: The record that was dropped, and the freed server IDs.

        Raises:
            ValidationAppError: `kind` is neither `nodes` nor `agents`.
            NotFoundError: That job has no run record.
        """
        scope = scope_of(kind, name)
        record = next(
            (r for r in await self._runs.list_all() if r.kind == kind and r.reported_by == name),
            None,
        )
        if record is None:
            raise NotFoundError(f"No {kind} membership job named {name!r} has reported.")
        freed = await OpenShiftMembershipService(
            server_repo=self._servers, audit=self._audit, actor=self._actor
        ).release(scope)
        await self._runs.delete(kind, name)
        await self._audit.record(
            EventType.CLUSTER_DECOMMISSIONED,
            actor=self._actor,
            data={
                "kind": kind,
                "name": name,
                "freed": len(freed),
                "last_run_at": record.finished_at.isoformat(),
            },
        )
        return record, freed

    async def notify_silent(self, *, now: datetime, silent_after_seconds: int) -> list[ClusterJob]:
        """
        Record `CLUSTER_NOT_REPORTING` for each job that has just gone silent.

        Once per silent stretch: the run record carries a mark that the next real run clears,
        and the mark is taken atomically, so several API replicas cannot record it twice.

        Args:
            now (datetime): The comparison instant.
            silent_after_seconds (int): How long without a run counts as silent.

        Returns:
            list[ClusterJob]: The jobs an event was recorded for this call.
        """
        cutoff = now - timedelta(seconds=silent_after_seconds)
        notified: list[ClusterJob] = []
        for job in await self.list_jobs(now=now, silent_after_seconds=silent_after_seconds):
            if not job.silent or not await self._runs.claim_not_reporting(
                job.kind, job.name, cutoff=cutoff
            ):
                continue
            try:
                await self._audit.record(
                    EventType.CLUSTER_NOT_REPORTING,
                    actor=self._actor,
                    data={
                        "kind": job.kind,
                        "name": job.name,
                        "held": job.held,
                        "last_run_at": job.last_run_at.isoformat(),
                        "silent_seconds": job.silent_seconds,
                    },
                )
            except Exception:
                await self._runs.release_not_reporting(job.kind, job.name)
                raise
            notified.append(job)
        return notified
