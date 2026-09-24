"""CLI: report which servers this cluster is using.

Runs *inside* a cluster, not against one. Deployed per-cluster by ArgoCD
(see `cronjobs/`), it reads its own API server through the pod's
ServiceAccount and writes `Server.openshift` for the servers it holds.

Two sources, one per CronJob:

    python3 -m tools.collect_openshift --source nodes
    python3 -m tools.collect_openshift --source agents

`nodes` runs in every cluster and reports its own worker nodes. `agents`
runs on an MCE hub and reports its Agents — bound to a hosted cluster, or
sitting in the hub's inventory bound to nothing.

Deliberately *not* a `ManagerType` inside `tools.run_collector`. That
tool's `_PROVIDER_FACTORIES` maps to `ServerInventoryProvider`, whose
whole contract is `list_servers() -> ProviderServer` — hardware records.
This produces no hardware and creates no servers: a cluster host that
matches nothing in the inventory is reported and skipped, because the
vendor collectors are the only thing entitled to say what exists.

Exit codes follow `run_collector`'s: 0 complete, 1 total failure,
2 not configured, 3 partial (some hosts matched nothing).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

import structlog

from app.application.services.audit_service import AuditService
from app.application.services.openshift_membership import (
    MembershipSummary,
    OpenShiftMembershipService,
)
from app.config import get_settings
from app.domain.models.audit_event import Actor, ActorType
from app.domain.models.openshift import MembershipRun
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.mongodb.membership_run_repository import MongoMembershipRunRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.openshift.client import (
    ClusterUnreadableError,
    InClusterClient,
    in_cluster_client,
)
from app.infrastructure.openshift.records import (
    ClusterObservation,
    agent_observation,
    name_excluded,
    node_observation,
)
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the command line.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Report which servers this cluster is using.")
    parser.add_argument(
        "--source",
        required=True,
        choices=("nodes", "agents"),
        help="nodes: this cluster's own worker nodes. agents: this MCE's Agents.",
    )
    parser.add_argument(
        "--cluster",
        default=None,
        help="Cluster name to record. Defaults to INVENTORY_OPENSHIFT_CLUSTER_NAME.",
    )
    parser.add_argument(
        "--mce-name",
        default=None,
        help="MCE to record, for --source agents. Defaults to INVENTORY_OPENSHIFT_MCE_NAME.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and correlate, write nothing.",
    )
    return parser.parse_args(argv)


async def _observe(
    client: InClusterClient,
    *,
    source: str,
    cluster_name: str,
    mce_name: str,
    exclude_name_parts: tuple[str, ...],
) -> list[ClusterObservation]:
    """
    Read this cluster and turn what it says into observations.

    Args:
        client (InClusterClient): Reader for this pod's own cluster.
        source (str): `nodes` or `agents`.
        cluster_name (str): Cluster to record on node observations.
        mce_name (str): MCE to record on agent observations.
        exclude_name_parts (tuple[str, ...]): Hostname substrings to drop
            — the one filter for both sources, applied to the resolved
            hostname either ends up with (ADR-0024's 2026-09-22 update).

    Returns:
        list[ClusterObservation]: Every host this cluster reported, minus
            the ones with no usable hostname and the excluded ones.

    Raises:
        ClusterUnreadableError: If the cluster could not be fully read.
            Never a partial list — the caller frees whatever it does not
            see, so a truncated read would free real servers.
    """
    if source == "nodes":
        nodes = await client.worker_nodes()
        seen = [node_observation(node, cluster_name=cluster_name) for node in nodes]
    else:
        agents = await client.agents()
        seen = [agent_observation(agent, mce_name=mce_name) for agent in agents]

    no_hostname = 0
    excluded_by_name = 0
    kept: list[ClusterObservation] = []
    for observation in seen:
        if observation is None:
            no_hostname += 1
        elif name_excluded(observation.hostname, exclude_name_parts):
            excluded_by_name += 1
        else:
            kept.append(observation)
    logger.info(
        "openshift.hosts_observed",
        source=source,
        listed=len(seen),
        kept=len(kept),
        no_hostname=no_hostname,
        excluded_by_name=excluded_by_name,
    )
    return kept


async def _record_run(
    repo: MongoMembershipRunRepository,
    *,
    source: str,
    reported_by: str,
    started_at: datetime,
    summary: MembershipSummary,
) -> None:
    """
    Write this run's outcome for the fleet gauges (ADR-0029's 2026-09-24 update).

    Never raises: the exit code must reflect the reconcile, not this write.

    Args:
        repo (MongoMembershipRunRepository): Where the record goes.
        source (str): `nodes` or `agents` — this run's `MembershipRun.kind`.
        reported_by (str): The cluster or MCE that reported.
        started_at (datetime): When the run began.
        summary (MembershipSummary): What `reconcile` returned.
    """
    run = MembershipRun(
        kind=source,
        reported_by=reported_by,
        finished_at=utcnow(),
        duration_seconds=(utcnow() - started_at).total_seconds(),
        observed=summary.observed,
        matched=summary.matched,
        unmatched=len(summary.unmatched),
        partial=bool(summary.unmatched),
    )
    try:
        await repo.record_run(run)
    except Exception as exc:
        logger.warning("openshift.record_run_failed", reported_by=reported_by, error=str(exc))


def _report(summary: MembershipSummary, *, reported_by: str, dry_run: bool) -> int:
    """
    Print what the run did and decide its exit code.

    Args:
        summary (MembershipSummary): The reconcile result.
        reported_by (str): The cluster or MCE that reported.
        dry_run (bool): Whether anything was actually written.

    Returns:
        int: 0 when every reported host matched a server, 3 when some did
            not — the same PARTIAL contract `run_collector` uses, because
            an unmatched host means the inventory's view is incomplete,
            not that the run failed.
    """
    prefix = "would " if dry_run else ""
    print(f"\n=== {reported_by} ===")
    print(f"  reported by cluster : {summary.observed}")
    print(f"  matched a server    : {summary.matched}")
    print(f"  {prefix}claim/update      : {summary.claimed}")
    print(f"  {prefix}free              : {summary.freed}")

    if not summary.unmatched:
        print("  unmatched hostnames : none")
        return 0

    # The match rate is the number to look at on a first run: a Dell
    # server whose requested hostname was never set reports a MAC-derived
    # name that matches nothing, and would look free rather than in use.
    rate = summary.matched / summary.observed if summary.observed else 0.0
    print(f"  unmatched hostnames : {len(summary.unmatched)} (match rate {rate:.0%})")
    for hostname in sorted(summary.unmatched)[:50]:
        print(f"    - {hostname}")
    if len(summary.unmatched) > 50:
        print(f"    … and {len(summary.unmatched) - 50} more")
    return 3


async def _run(*, source: str, cluster: str | None, mce_name: str | None, dry_run: bool) -> int:
    """
    Read this cluster and reconcile the inventory against it.

    Args:
        source (str): `nodes` or `agents`.
        cluster (str | None): Cluster name override.
        mce_name (str | None): MCE override.
        dry_run (bool): Read and correlate, write nothing.

    Returns:
        int: 0 complete, 1 total failure, 2 not configured, 3 partial.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )

    cluster_name = (cluster or settings.openshift_cluster_name).strip()
    mce = (mce_name or settings.openshift_mce_name).strip()

    # Checked before any connection, so a half-configured deployment gets
    # the variable to set rather than a run that reports zero and frees
    # the fleet.
    if source == "nodes" and not cluster_name:
        print(
            "--source nodes needs the cluster's name, to record on the servers it "
            "holds and to scope what it may release. Set --cluster or "
            "INVENTORY_OPENSHIFT_CLUSTER_NAME.",
            file=sys.stderr,
        )
        return 2
    if source == "agents" and not mce:
        print(
            "--source agents needs this MCE's name, to record on the servers it "
            "holds and to scope what it may release. Set --mce-name or "
            "INVENTORY_OPENSHIFT_MCE_NAME.",
            file=sys.stderr,
        )
        return 2

    reported_by = cluster_name if source == "nodes" else mce
    structlog.contextvars.bind_contextvars(source=source, reported_by=reported_by)
    exclude = tuple(
        part.strip().lower()
        for part in settings.openshift_exclude_name_parts.split(",")
        if part.strip()
    )

    started_at = utcnow()
    try:
        async with in_cluster_client(
            timeout_seconds=settings.openshift_request_timeout_seconds
        ) as http:
            observations = await _observe(
                InClusterClient(http),
                source=source,
                cluster_name=cluster_name,
                mce_name=mce,
                exclude_name_parts=exclude,
            )
    except ClusterUnreadableError as exc:
        # Fatal, and before any write: the reconcile frees what this
        # cluster does not report, so a failed read must not reach it.
        logger.exception("openshift.cluster_unreadable", error=str(exc))
        print(f"cluster could not be read: {exc}", file=sys.stderr)
        return 1

    # An empty answer is far more likely a broken selector or an RBAC
    # change than an emptied cluster, and acting on it frees everything.
    if not observations:
        logger.error("openshift.nothing_reported", reported_by=reported_by)
        print(
            f"{reported_by} reported nothing. Refusing to release the servers it "
            "holds: an empty answer is far more likely a broken query or a "
            "permissions change than a genuinely empty cluster.",
            file=sys.stderr,
        )
        return 1

    scope: dict[str, object] = (
        {"openshift.cluster_name": cluster_name}
        if source == "nodes"
        else {"openshift.mce_name": mce}
    )

    mongo = MongoClientHolder(settings)
    await mongo.connect()
    try:
        await ensure_indexes(mongo.db)
        service = OpenShiftMembershipService(
            server_repo=MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            audit=AuditService(repo=MongoAuditEventRepository(mongo)),
            actor=Actor(type=ActorType.SYSTEM, id=f"openshift:{reported_by}"),
        )
        summary = await service.reconcile(
            observations,
            scope=scope,
            reported_by=reported_by,
            dry_run=dry_run,
        )
        if not dry_run:
            await _record_run(
                MongoMembershipRunRepository(mongo),
                source=source,
                reported_by=reported_by,
                started_at=started_at,
                summary=summary,
            )
    finally:
        await mongo.close()

    return _report(summary, reported_by=reported_by, dry_run=dry_run)


def main(argv: list[str] | None = None) -> None:
    """
    Entry point: parse args, run, exit with the status code.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Raises:
        SystemExit: With the run's exit code.
    """
    args = _parse_args(argv)
    raise SystemExit(
        asyncio.run(
            _run(
                source=args.source,
                cluster=args.cluster,
                mce_name=args.mce_name,
                dry_run=args.dry_run,
            )
        )
    )


if __name__ == "__main__":
    main()
