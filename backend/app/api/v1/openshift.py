"""`/api/v1/openshift/clusters`: membership jobs, and retiring a destroyed one (ADR-0067)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v1.openshift_schemas import ClusterJobItem, ClusterJobsResponse, DecommissionResponse
from app.application.services.audit_service import AuditService
from app.application.services.cluster_decommission import ClusterDecommissionService
from app.config import Settings, get_settings
from app.dependencies import get_mongo_holder, get_redis_holder, get_request_id, require_admin
from app.domain.models.audit_event import Actor, ActorType
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.membership_run_repository import MongoMembershipRunRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.cache import CacheClient
from app.infrastructure.redis.client import RedisClientHolder
from app.infrastructure.redis.keys import list_cache_patterns, server_cache_patterns
from app.utils.timeutil import utcnow

router = APIRouter(
    prefix="/api/v1/openshift", tags=["openshift"], dependencies=[Depends(require_admin)]
)


def _service(
    mongo: MongoClientHolder, settings: Settings, actor: Actor
) -> ClusterDecommissionService:
    """
    Build the service for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.
        settings (Settings): Supplies the cursor-signing secret.
        actor (Actor): The caller, for the audit events.

    Returns:
        ClusterDecommissionService: A service bound to those dependencies.
    """
    return ClusterDecommissionService(
        server_repo=MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
        runs=MongoMembershipRunRepository(mongo),
        audit=AuditService(repo=MongoAuditEventRepository(mongo)),
        actor=actor,
    )


@router.get("/clusters", response_model=ClusterJobsResponse)
async def list_cluster_jobs(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ClusterJobsResponse:
    """
    List every membership job with the servers it holds and how long since it last ran.

    Admin only, like the decommission (the whole router requires it, ADR-0067).

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.
        settings (Settings): Supplies the silence threshold.

    Returns:
        ClusterJobsResponse: Jobs, longest silent first; `silent` is true past the threshold.
    """
    service = _service(mongo, settings, Actor(type=ActorType.SYSTEM, id="read"))
    jobs = await service.list_jobs(
        now=utcnow(), silent_after_seconds=settings.openshift_silent_after_seconds
    )
    return ClusterJobsResponse(
        items=[
            ClusterJobItem(
                kind=j.kind,
                name=j.name,
                held=j.held,
                last_run_at=j.last_run_at,
                silent_seconds=j.silent_seconds,
                silent=j.silent,
            )
            for j in jobs
        ],
        silent_after_seconds=settings.openshift_silent_after_seconds,
    )


@router.post("/clusters/{kind}/{name}/decommission", response_model=DecommissionResponse)
async def decommission_cluster(
    kind: str,
    name: str,
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    redis: Annotated[RedisClientHolder, Depends(get_redis_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[Actor, Depends(require_admin)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> DecommissionResponse:
    """
    Retire a destroyed cluster's membership job (ADR-0067): free its servers, drop its run record.

    Args:
        kind (str): `nodes` or `agents`.
        name (str): The cluster or MCE name.
        mongo (MongoClientHolder): The shared Mongo client holder.
        redis (RedisClientHolder): For the cache invalidation.
        settings (Settings): Supplies the cursor-signing secret.
        actor (Actor): The admin to attribute the events to.
        request_id (str | None): The current request's ID.

    Returns:
        DecommissionResponse: How many servers were freed.

    Raises:
        ValidationAppError: `kind` is neither `nodes` nor `agents`.
        NotFoundError: That job has no run record.
    """
    _, freed = await _service(mongo, settings, actor).decommission(kind, name)
    cache = CacheClient(redis)
    await cache.delete_matching(
        *list_cache_patterns(), *(p for sid in freed for p in server_cache_patterns(sid))
    )
    return DecommissionResponse(kind=kind, name=name, freed=len(freed))
