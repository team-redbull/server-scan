"""`/api/v1/health-policies`: read-only listing and lookup, and `/api/v1/health-metrics`.

Follows the same thin-router pattern as `app.api.v1.servers`: dependency
providers construct repositories/services from what's already on
`app.state` (never a module-level global holder), routes stay free of
business logic, and every domain-facing error is an `AppError` subclass —
never a raw `HTTPException` — so it renders through the shared RFC 9457
handler in `app.exception_handlers`.

Health policies are read-only (`27b20a8`, `f9ab059`): they ship with the
platform and are seeded/validated at startup
(`app.application.services.bootstrap`, via
`app.application.services.health_policy_service.validate_policy_write`),
never created or edited through this router.

The metric registry is built once at import time (`_METRIC_REGISTRY`)
rather than per-request or off `app.state`: `build_default_registry()` is
a pure, deterministic function over a fixed set of ~11 core metric
definitions (see `app.domain.services.health.metrics`'s own docstring on
why it's constructed explicitly rather than kept as global mutable
state) — a module-level constant here is that same "construct once,
pass explicitly" discipline, just scoped to this router instead of
`app.main`'s lifespan, since nothing about it depends on settings or a
live connection the way Mongo/Redis holders do.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.health_policy_schemas import (
    HealthMetricListResponse,
    HealthMetricResponse,
    HealthPolicyListResponse,
    HealthPolicyResponse,
)
from app.dependencies import get_mongo_holder
from app.domain.services.health.metrics import MetricRegistry, build_default_registry
from app.errors import NotFoundError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository

router = APIRouter(prefix="/api/v1", tags=["health-policies"])

_METRIC_REGISTRY = build_default_registry()


async def _metric_registry() -> MetricRegistry:
    """
    Return the module-level metric registry built once at import time.

    Returns:
        MetricRegistry: The shared, immutable registry.
    """
    return _METRIC_REGISTRY


async def _policy_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoHealthPolicyRepository:
    """
    Build the health policy repository for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.

    Returns:
        MongoHealthPolicyRepository: A repository bound to that client.
    """
    return MongoHealthPolicyRepository(mongo)


@router.get("/health-policies", response_model=HealthPolicyListResponse)
async def list_policies(
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
    enabled: bool | None = Query(default=None),
) -> HealthPolicyListResponse:
    """
    List every health policy, optionally filtered by enabled state.

    Args:
        policy_repo (MongoHealthPolicyRepository): The health policy repository.
        enabled (bool | None): When set, restrict to enabled or disabled
            policies only; omit to return every policy.

    Returns:
        HealthPolicyListResponse: The matching policies.
    """
    # Three states: `bool(enabled)` collapsed `?enabled=false` into "no filter".
    if enabled is None:
        policies = await policy_repo.list_all()
    elif enabled:
        policies = await policy_repo.list_all(enabled_only=True)
    else:
        policies = [p for p in await policy_repo.list_all() if not p.enabled]
    return HealthPolicyListResponse(items=[HealthPolicyResponse.from_policy(p) for p in policies])


@router.get("/health-policies/{policy_id}", response_model=HealthPolicyResponse)
async def get_policy(
    policy_id: str,
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
) -> HealthPolicyResponse:
    """
    Get one health policy by ID.

    Args:
        policy_id (str): The policy's ID.
        policy_repo (MongoHealthPolicyRepository): The health policy repository.

    Returns:
        HealthPolicyResponse: The matching policy.

    Raises:
        NotFoundError: No policy has that ID.
    """
    policy = await policy_repo.get_by_id(policy_id)
    if policy is None:
        raise NotFoundError(
            f"No health policy with id {policy_id!r}.", details={"policy_id": policy_id}
        )
    return HealthPolicyResponse.from_policy(policy)


@router.get("/health-metrics", response_model=HealthMetricListResponse, tags=["health-metrics"])
async def list_health_metrics(
    registry: Annotated[MetricRegistry, Depends(_metric_registry)],
) -> HealthMetricListResponse:
    """
    List every metric definition the health policy engine can evaluate.

    Args:
        registry (MetricRegistry): The module-level metric registry.

    Returns:
        HealthMetricListResponse: Every registered metric definition.
    """
    return HealthMetricListResponse(
        items=[
            HealthMetricResponse(
                name=m.name,
                type=m.type,
                category=m.category,
                description=m.description,
                enum_values=list(m.enum_values) if m.enum_values is not None else None,
                provider=m.provider,
            )
            for m in registry.all()
        ]
    )
