"""Application entry point.

Startup order matters and is made explicit here rather than left to import
side effects: settings -> logging -> Mongo -> Redis -> ready. Each later
step can log through the structured logger because logging is configured
first; Mongo is connected before Redis because Mongo is the hard dependency
(startup fails if it's unreachable) while Redis is a soft one (startup
continues, readiness reports it as degraded).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response

from app.api.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.classification_rules import router as classification_rules_router
from app.api.v1.events import router as events_router
from app.api.v1.health_policies import router as health_policies_router
from app.api.v1.servers import router as servers_router
from app.api.v1.sites import router as sites_router
from app.application.services.bootstrap import (
    ensure_default_classification_rules,
    ensure_default_health_policies,
)
from app.config import get_settings
from app.dependencies import get_current_actor
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.site import site_catalog
from app.exception_handlers import register_exception_handlers
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.membership_run_repository import MongoMembershipRunRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis import RedisClientHolder
from app.infrastructure.singleflight import drain as drain_singleflight
from app.middleware.request_context import RequestContextMiddleware
from app.observability.fleet_gauges import FleetGaugeRefresher
from app.observability.metrics import http_request_duration_seconds, http_requests_total

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Start and stop the app's own resources, in dependency order.

    Settings -> logging -> Mongo -> bootstrap defaults -> Redis up; the
    reverse down, after draining in-flight coalesced computations (ADR-0007).

    Args:
        app (FastAPI): The application being started, to stash the
            connected clients on (`app.state.mongo`/`.redis`).

    Yields:
        None: Control, for the app's request-serving lifetime.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    logger.info("app.starting", environment=settings.environment)

    mongo = MongoClientHolder(settings)
    await mongo.connect()
    await ensure_indexes(mongo.db)
    regex_engine = RegexModuleEngine(
        max_pattern_length=settings.regex_max_pattern_length,
        match_timeout_seconds=settings.regex_match_timeout_seconds,
    )
    await ensure_default_classification_rules(
        MongoClassificationRuleRepository(mongo),
        site_catalog(settings.sites),
        engine=regex_engine,
    )
    await ensure_default_health_policies(
        MongoHealthPolicyRepository(mongo), registry=build_default_registry()
    )
    app.state.mongo = mongo
    app.state.fleet_gauges = FleetGaugeRefresher(
        MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
        MongoManagerRepository(mongo),
        MongoMembershipRunRepository(mongo),
        stale_after_seconds=settings.stale_after_seconds,
        min_interval_seconds=settings.metrics_fleet_refresh_seconds,
    )

    redis = RedisClientHolder(settings)
    await redis.connect()
    app.state.redis = redis

    logger.info("app.ready")
    try:
        yield
    finally:
        logger.info("app.stopping")
        # Before the clients close — see `singleflight.drain`.
        await drain_singleflight()
        await redis.close()
        await mongo.close()


def create_app() -> FastAPI:
    """
    Build the FastAPI app: middleware, exception handlers, every router.

    Returns:
        FastAPI: A fully configured, not-yet-started application.
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    # Level 6, not the default 9: 15.8x for 0.86ms vs 16.1x for 1.57ms
    # on a 200-row page (docs/architecture.md, "Search, pagination, and caching").
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    # Every route below requires a resolved caller (docs/adr/0034); `/health`,
    # `/auth/*` and `/metrics` stay open.
    _authenticated = [Depends(get_current_actor)]
    app.include_router(servers_router, dependencies=_authenticated)
    app.include_router(classification_rules_router, dependencies=_authenticated)
    app.include_router(health_policies_router, dependencies=_authenticated)
    app.include_router(events_router, dependencies=_authenticated)
    app.include_router(sites_router, dependencies=_authenticated)

    if settings.metrics_enabled:

        @app.middleware("http")
        async def record_metrics(request: Request, call_next: RequestResponseEndpoint) -> Response:
            start = time.monotonic()
            response = await call_next(request)
            duration = time.monotonic() - start
            route = request.scope.get("route")
            # `None` on a 404; a sentinel bounds label cardinality (architecture.md).
            path_label = route.path if route is not None else "<unmatched>"
            http_requests_total.labels(
                method=request.method, path=path_label, status=response.status_code
            ).inc()
            http_request_duration_seconds.labels(method=request.method, path=path_label).observe(
                duration
            )
            return response

        @app.get("/metrics", include_in_schema=False)
        async def metrics(request: Request) -> Response:
            # Fleet gauges come from MongoDB, throttled — ADR-0029.
            refresher: FleetGaugeRefresher | None = getattr(request.app.state, "fleet_gauges", None)
            if refresher is not None:
                await refresher.maybe_refresh()
            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
