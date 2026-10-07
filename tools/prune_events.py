"""CLI: delete audit events older than the retention window (ADR-0045).

**Report-only by default**: it logs how many events the cutoff would delete and
deletes nothing until `INVENTORY_AUDIT_RETENTION_REPORT_ONLY=false` or `--apply`.
`INVENTORY_AUDIT_RETENTION_DAYS=0` keeps every event forever.

Usage:
    uv run python -m tools.prune_events [--apply] [--retention-days N]

Exit codes: 0 done, report-only or disabled, 1 invalid config or failure.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from app.application.services.audit_service import AuditService
from app.config import get_settings
from app.domain.models.audit_event import Actor, ActorType, EventType
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import (
    MongoAuditEventRepository,
    PurgeProgress,
)
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

RETENTION_ACTOR = Actor(type=ActorType.SYSTEM, id="audit-retention")
REASON = "age-based retention"
_MIN_RETENTION_DAYS = 7


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """What one retention pass found or did."""

    cutoff: datetime
    report_only: bool
    would_delete: int = 0
    deleted: int = 0
    batches: int = 0
    truncated: bool = False


async def prune_events(
    *,
    repo: MongoAuditEventRepository,
    audit: AuditService,
    retention_days: int,
    report_only: bool,
    batch_size: int,
    max_batches: int,
    now: datetime,
) -> RetentionOutcome:
    """
    Run one retention pass: preview, then (unless report-only) purge and record one event.

    Args:
        repo (MongoAuditEventRepository): The audit events collection.
        audit (AuditService): Records the single `AUDIT_PURGED` event after a purge.
        retention_days (int): Events older than this many days are deleted.
        report_only (bool): Log what would be deleted and delete nothing.
        batch_size (int): Events per delete batch.
        max_batches (int): Most batches one pass may run.
        now (datetime): The current time.

    Returns:
        RetentionOutcome: The preview count (report-only) or the purge totals.
    """
    cutoff = now - timedelta(days=retention_days)
    preview = await repo.preview_before(cutoff)
    if report_only:
        logger.info(
            "audit_retention.report_only",
            would_delete=preview.count,
            oldest=preview.oldest,
            newest=preview.newest,
            cutoff=cutoff.isoformat(),
            retention_days=retention_days,
        )
        return RetentionOutcome(cutoff=cutoff, report_only=True, would_delete=preview.count)
    progress = PurgeProgress()
    try:
        purged = await repo.purge_before(
            cutoff, batch_size=batch_size, max_batches=max_batches, progress=progress
        )
    except Exception:
        # Earlier batches are already gone: leave a record of them even though the run failed.
        if progress.deleted > 0:
            await _record_purge(
                audit, cutoff, retention_days, progress, truncated=True, complete=False
            )
        raise
    logger.info(
        "audit_retention.purged",
        deleted=purged.deleted,
        batches=purged.batches,
        cutoff=cutoff.isoformat(),
        truncated=purged.truncated,
    )
    if purged.deleted > 0:
        await _record_purge(
            audit, cutoff, retention_days, progress, truncated=purged.truncated, complete=True
        )
    return RetentionOutcome(
        cutoff=cutoff,
        report_only=False,
        deleted=purged.deleted,
        batches=purged.batches,
        truncated=purged.truncated,
    )


async def _record_purge(
    audit: AuditService,
    cutoff: datetime,
    retention_days: int,
    progress: PurgeProgress,
    *,
    truncated: bool,
    complete: bool,
) -> None:
    """
    Record the one `AUDIT_PURGED` event for a purge, whole or partial.

    Args:
        audit (AuditService): Writes the event.
        cutoff (datetime): The cutoff the purge used.
        retention_days (int): The configured retention.
        progress (PurgeProgress): What was deleted.
        truncated (bool): Whether events older than the cutoff remain.
        complete (bool): False when the purge raised partway; the failure itself is logged.
    """
    try:
        await audit.record(
            EventType.AUDIT_PURGED,
            actor=RETENTION_ACTOR,
            data={
                "cutoff": cutoff.isoformat(),
                "retention_days": retention_days,
                "deleted": progress.deleted,
                "batches": progress.batches,
                "truncated": truncated,
                "complete": complete,
                "oldest_deleted_created_at": progress.oldest,
                "newest_deleted_created_at": progress.newest,
                "reason": REASON,
            },
        )
    except Exception:
        logger.exception("audit_retention.record_failed", deleted=progress.deleted)
        if complete:
            raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse this CLI's arguments.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: The parsed `--apply` and `--retention-days` values.
    """
    parser = argparse.ArgumentParser(description="Delete audit events past the retention window.")
    parser.add_argument("--apply", action="store_true", help="Delete (default is report-only).")
    parser.add_argument("--retention-days", type=int, default=None)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """
    Connect, run one pass and print a summary.

    Args:
        args (argparse.Namespace): The parsed CLI arguments.

    Returns:
        int: Exit code, 0 done/report-only/disabled and 1 on invalid config or failure.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    days = args.retention_days if args.retention_days is not None else settings.audit_retention_days
    if days == 0:
        logger.info("audit_retention.disabled", retention_days=0)
        print("audit retention disabled (retention days is 0): nothing to do")
        return 0
    if days < _MIN_RETENTION_DAYS:
        logger.error("audit_retention.invalid_config", retention_days=days)
        return 1
    apply = args.apply or not settings.audit_retention_report_only
    mongo = MongoClientHolder(settings)
    try:
        await mongo.connect()
        repo = MongoAuditEventRepository(mongo)
        outcome = await prune_events(
            repo=repo,
            audit=AuditService(repo=repo),
            retention_days=days,
            report_only=not apply,
            batch_size=settings.audit_retention_batch_size,
            max_batches=settings.audit_retention_max_batches,
            now=utcnow(),
        )
    except Exception:
        logger.exception("audit_retention.failed")
        return 1
    finally:
        await mongo.close()
    if outcome.report_only:
        print(
            f"audit retention REPORT ONLY (nothing deleted): would_delete={outcome.would_delete} "
            f"cutoff={outcome.cutoff.isoformat()} retention_days={days}"
        )
    else:
        print(
            f"audit retention APPLIED: deleted={outcome.deleted} batches={outcome.batches} "
            f"truncated={outcome.truncated} cutoff={outcome.cutoff.isoformat()}"
        )
    return 0


def main(argv: list[str] | None = None) -> None:
    """
    Entry point: parse args, run the pass, exit with its status code.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Raises:
        SystemExit: With `_run`'s exit code.
    """
    raise SystemExit(asyncio.run(_run(_parse_args(argv))))


if __name__ == "__main__":
    main()
