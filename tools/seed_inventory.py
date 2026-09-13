"""CLI: seed MongoDB with deterministic fake inventory data.

Usage:
    uv run python -m tools.seed_inventory --count 1000 --seed 42

Runs the exact same ingestion pipeline (`app.application.services.ingest.
IngestService`) a real collector would go through — this is a seeding
convenience, not a shortcut that writes `Server` documents directly.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import structlog

from app.application.services.audit_service import AuditService
from app.application.services.bootstrap import (
    ensure_default_classification_rules,
    ensure_default_health_policies,
)
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService
from app.config import get_settings
from app.domain.enums import ManagerType, OpenShiftState
from app.domain.models.manager import ManagerRun
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.gpu_catalog import gpu_catalog
from app.domain.value_objects.site import site_catalog
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import list_managers, list_sites, manager_id_for
from app.infrastructure.providers.fake.openshift import openshift_for
from app.infrastructure.providers.fake.provider import fake_providers
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse this CLI's arguments.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: The parsed `--count`/`--seed` values.
    """
    parser = argparse.ArgumentParser(
        description="Seed the inventory database with deterministic fake data."
    )
    parser.add_argument(
        "--count", type=int, default=1000, help="Number of fake servers to generate."
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible output.")
    return parser.parse_args(argv)


async def _run(*, count: int, seed: int) -> None:
    """
    Seed default classification rules/health policies, then ingest `count` fake servers.

    Args:
        count (int): How many fake servers to generate.
        seed (int): Random seed, for reproducible output across runs.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )

    mongo = MongoClientHolder(settings)
    await mongo.connect()
    try:
        await ensure_indexes(mongo.db)
        # Also seeded at app startup; repeated here for a database no API has touched.
        rule_repo = MongoClassificationRuleRepository(mongo)
        policy_repo = MongoHealthPolicyRepository(mongo)
        sites = site_catalog(settings.sites)
        registry = build_default_registry()
        regex_engine = RegexModuleEngine(
            max_pattern_length=settings.regex_max_pattern_length,
            match_timeout_seconds=settings.regex_match_timeout_seconds,
        )
        await ensure_default_classification_rules(rule_repo, sites, engine=regex_engine)
        await ensure_default_health_policies(policy_repo, registry=registry)

        ingest_service = IngestService(
            server_repo=MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            site_repo=MongoSiteRepository(mongo),
            manager_repo=MongoManagerRepository(mongo),
            sites=sites,
            gpu_catalog=gpu_catalog(settings.gpu_models),
            classification_service=ClassificationService(rule_repo=rule_repo, engine=regex_engine),
            health_service=HealthPolicyService(
                policy_repo=policy_repo,
                registry=registry,
            ),
            audit=AuditService(repo=MongoAuditEventRepository(mongo)),
        )
        # One pass per collector, so `Server.source_provider` varies across the fleet.
        fetched = created = updated = errors = 0
        manager_repo = MongoManagerRepository(mongo)
        for provider in fake_providers(seed=seed, count=count, sites=sites):
            started_at = utcnow()
            started = time.monotonic()
            summary = await ingest_service.ingest(
                provider, sites=list_sites(sites), managers=list_managers()
            )
            fetched += summary.fetched
            created += summary.created
            updated += summary.updated
            errors += summary.errors
            # So the seeded cluster shows the `collector_last_run_*` gauges (ADR-0029).
            await manager_repo.record_run(
                manager_id_for(ManagerType(provider.provider_type)),
                ManagerRun(
                    started_at=started_at,
                    finished_at=utcnow(),
                    duration_seconds=time.monotonic() - started,
                    servers_fetched=summary.fetched,
                    servers_created=summary.created,
                    servers_updated=summary.updated,
                    ingest_errors=summary.errors,
                    collection_errors=len(provider.collection_errors),
                    partial=bool(summary.errors),
                ),
            )

        reported = await _seed_openshift(
            MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        )

        logger.info(
            "seed.completed",
            fetched=fetched,
            created=created,
            updated=updated,
            errors=errors,
            openshift_reported=reported,
        )
        print(f"fetched={fetched} created={created} updated={updated} errors={errors}")
    finally:
        await mongo.close()


async def _seed_openshift(repo: MongoServerRepository) -> int:
    """
    Stand in for the two OpenShift jobs, over the fleet just seeded.

    A second pass: a `ProviderServer` has no `openshift` field, and seeding
    it through a collector would model a data path that does not exist.

    Args:
        repo (MongoServerRepository): Where the fleet was just written.

    Returns:
        int: How many servers a cluster or an MCE reported on — the
            `AVAILABLE` ones are not counted, since nothing holds them.
    """
    reported = 0
    cursor: str | None = None
    while True:
        page = await repo.list_page(
            filters={},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=cursor,
            page_size=500,
            with_count=False,
        )
        for server in page.items:
            server.openshift = openshift_for(server)
            if server.openshift.lifecycle_state is not OpenShiftState.AVAILABLE:
                reported += 1
            await repo.upsert(server)
        if not page.has_more or page.next_cursor is None:
            break
        cursor = page.next_cursor
    return reported


def main(argv: list[str] | None = None) -> None:
    """
    Entry point: parse args and run the seeding pass.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.
    """
    args = _parse_args(argv)
    asyncio.run(_run(count=args.count, seed=args.seed))


if __name__ == "__main__":
    main()
