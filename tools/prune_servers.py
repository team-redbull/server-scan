"""CLI: delete servers their manager has stopped listing (ADR-0037 decision 6).

A server whose `listed_at` is older than the threshold is deleted, subject to
three guards per collector: its managers' latest runs must be clean and
recent, and one pass may not delete more than a fraction of its servers.
**Dry-run by default**; `--apply` or `INVENTORY_PRUNE_ENABLED=true` deletes.
Documents written before `listed_at` existed are stamped with "now" on the
first applied run and are never deleted by that run.

Usage:
    uv run python -m tools.prune_servers [--apply] [--older-than-seconds N]

Exit codes: 0 done (or dry-run), 1 failure.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from app.application.services.audit_service import AuditService
from app.config import get_settings
from app.domain.models.audit_event import Actor, ActorType, EventType
from app.domain.models.manager import Manager
from app.domain.models.server import Server
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis import RedisClientHolder
from app.infrastructure.redis.cache import CacheClient
from app.infrastructure.redis.keys import list_cache_patterns, server_cache_patterns
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

PRUNE_ACTOR = Actor(type=ActorType.SYSTEM, id="prune")
REASON = "not listed by its manager within the prune threshold"


@dataclass
class PruneResult:
    """What one pass did, per collector."""

    backfilled: int = 0
    deleted: list[str] = field(default_factory=list)
    would_delete: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def _run_is_trusted(managers: list[Manager], *, now: datetime, max_age_seconds: int) -> str | None:
    """
    Decide whether a collector's latest runs may back a deletion.

    Args:
        managers (list[Manager]): The collector's managers.
        now (datetime): The current time.
        max_age_seconds (int): How recent a run must be.

    Returns:
        str | None: Why the collector is untrusted, or `None` if it is trusted.
    """
    enabled = [m for m in managers if m.enabled]
    if not enabled:
        return "no enabled manager"
    for manager in enabled:
        run = manager.last_run
        if run is None:
            return f"manager {manager.id} has no recorded run"
        if run.partial or run.ingest_errors:
            return f"manager {manager.id} last run was not clean"
        if run.finished_at < now - timedelta(seconds=max_age_seconds):
            return f"manager {manager.id} last run is older than {max_age_seconds}s"
    return None


async def _delete_one(
    server: Server,
    *,
    repo: MongoServerRepository,
    audit: AuditService,
    cache: CacheClient | None,
) -> bool:
    """
    Audit, log and delete one server, then drop its cache entries.

    Args:
        server (Server): The server to prune.
        repo (MongoServerRepository): The servers collection.
        audit (AuditService): Where the `SERVER_PRUNED` event is recorded.
        cache (CacheClient | None): Invalidated after the delete, when given.

    Returns:
        bool: `True` if the document was deleted.
    """
    details = {
        "server_id": server.id,
        "name": server.name,
        "serial": server.identity.serial,
        "vendor": server.identity.vendor.value,
        "source_provider": server.source_provider,
        "manager_id": server.manager_id,
        "listed_at": server.listed_at.isoformat() if server.listed_at else None,
        "last_seen_at": server.last_seen_at.isoformat() if server.last_seen_at else None,
        "reason": REASON,
    }
    await audit.record(
        EventType.SERVER_PRUNED, actor=PRUNE_ACTOR, server_id=server.id, data=details
    )
    deleted = await repo.delete(server.id)
    logger.info("server.pruned", deleted=deleted, **details)
    if deleted and cache is not None:
        await cache.delete_matching(*server_cache_patterns(server.id), *list_cache_patterns())
    return deleted


async def prune(
    *,
    repo: MongoServerRepository,
    manager_repo: MongoManagerRepository,
    audit: AuditService,
    cache: CacheClient | None,
    apply: bool,
    now: datetime,
    older_than_seconds: int,
    max_fraction: float,
    max_run_age_seconds: int,
) -> PruneResult:
    """
    Run one guarded prune pass.

    Args:
        repo (MongoServerRepository): The servers collection.
        manager_repo (MongoManagerRepository): Source of each collector's last run.
        audit (AuditService): Records `SERVER_PRUNED` per deletion.
        cache (CacheClient | None): Invalidated on deletion, when given.
        apply (bool): Delete for real; otherwise report only.
        now (datetime): The current time.
        older_than_seconds (int): A `listed_at` older than this is stale.
        max_fraction (float): Most of one collector's servers one pass may delete.
        max_run_age_seconds (int): How recent a collector's last run must be.

    Returns:
        PruneResult: What was (or would be) deleted and which collectors were skipped.
    """
    result = PruneResult()
    result.backfilled = (
        await repo.backfill_listed_at(now) if apply else await repo.count_missing_listed_at()
    )
    candidates = await repo.list_listed_before(now - timedelta(seconds=older_than_seconds))
    if not candidates:
        return result
    totals = await repo.count_by_source_provider()
    managers = await manager_repo.list_all()

    by_provider: defaultdict[str, list[Server]] = defaultdict(list)
    for server in candidates:
        by_provider[server.source_provider or ""].append(server)

    for provider, stale in by_provider.items():
        untrusted = _run_is_trusted(
            [m for m in managers if m.type.value == provider],
            now=now,
            max_age_seconds=max_run_age_seconds,
        )
        if untrusted is None and len(stale) > max_fraction * totals.get(provider, 0):
            untrusted = f"{len(stale)} of {totals.get(provider, 0)} servers exceeds {max_fraction}"
        if untrusted is not None:
            result.skipped[provider] = untrusted
            logger.warning("prune.skipped", source_provider=provider, reason=untrusted)
            continue
        for server in stale:
            if not apply:
                result.would_delete.append(server.id)
                logger.info("server.prune_would_delete", server_id=server.id, name=server.name)
            elif await _delete_one(server, repo=repo, audit=audit, cache=cache):
                result.deleted.append(server.id)
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse this CLI's arguments.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: The parsed `--apply`/`--older-than-seconds`/`--max-fraction` values.
    """
    parser = argparse.ArgumentParser(description="Delete servers no manager lists any more.")
    parser.add_argument("--apply", action="store_true", help="Delete (default is a dry run).")
    parser.add_argument("--older-than-seconds", type=int, default=None)
    parser.add_argument("--max-fraction", type=float, default=None)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """
    Connect, run one pass and print a summary.

    Args:
        args (argparse.Namespace): The parsed CLI arguments.

    Returns:
        int: Exit code — 0 done, 1 failure.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    apply = args.apply or settings.prune_enabled
    mongo = MongoClientHolder(settings)
    redis = RedisClientHolder(settings)
    try:
        await mongo.connect()
        cache: CacheClient | None = None
        if apply:
            await redis.connect()
            cache = CacheClient(redis)
        result = await prune(
            repo=MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            manager_repo=MongoManagerRepository(mongo),
            audit=AuditService(repo=MongoAuditEventRepository(mongo)),
            cache=cache,
            apply=apply,
            now=utcnow(),
            older_than_seconds=args.older_than_seconds or settings.prune_after_seconds,
            max_fraction=(
                args.max_fraction if args.max_fraction is not None else settings.prune_max_fraction
            ),
            max_run_age_seconds=settings.prune_max_run_age_seconds,
        )
    except Exception:
        logger.exception("prune.failed")
        return 1
    finally:
        await mongo.close()
        await redis.close()
    mode = "APPLIED" if apply else "DRY RUN (nothing deleted; pass --apply)"
    print(
        f"prune {mode}: deleted={len(result.deleted)} would_delete={len(result.would_delete)} "
        f"legacy_without_listed_at={result.backfilled} skipped={result.skipped}"
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
