"""`GET /api/v1/servers`, `GET /api/v1/servers/{server_id}`.

Filters are deliberately *not* individual typed FastAPI query parameters:
FastAPI silently drops any query param that isn't bound to a declared
function parameter, which would make it impossible to ever reach
`UNKNOWN_FILTER` — the whole point of that error code is to reject a
caller-supplied filter key that isn't in the whitelist, not to ignore it.
So every query param *except* the fixed non-filter set (`search`, `sort`,
`sort_desc`, `cursor`, `page_size`, `with_count`) is collected generically
from `request.query_params` and run through
`app.domain.services.search.build_filter_query`, which is the single
place that knows the whitelist and raises `UnknownFilterError` for
anything outside it.

Caching: list pages are cache-aside under `list_key(...)` — the key is
fully computable from the request itself (filter/search/sort/cursor hash),
so there's no bootstrapping problem. They carry a short TTL and no
invalidation on ingest; the two maintenance endpoints are the one
exception (`_invalidate_list_cache`). ADR-0028 has the reasoning.

Server detail is trickier: the key
design in `app.infrastructure.redis.keys.server_key` embeds the document's
`revision` specifically so a write never needs an explicit invalidation
call, but that means the *current* revision has to be known before the
real cache key can even be built — and the closed `ServerRepository`
port (`app.domain.ports.repository`, out of this slice's scope to modify)
has no cheap revision-only lookup, only a full `get_by_id`. This module
resolves that with a small self-maintained pointer entry
(`_revision_pointer_key`, id -> current revision, same TTL as the detail
payload) written through the same `CacheClient` — a normal cache-aside
read still degrades to Mongo on any Redis failure, it just costs one
extra (also-degrading) cache read on the hot path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.v1.maintenance_schemas import MaintenanceEnableRequest
from app.api.v1.schemas import (
    PageInfo,
    ServerDetail,
    ServerFacets,
    ServerListResponse,
    ServerSummary,
)
from app.application.services.audit_service import AuditService
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.maintenance_service import MaintenanceService
from app.application.services.pipeline import classification_from_result, health_from_state
from app.config import Settings, get_settings
from app.dependencies import get_current_actor, get_mongo_holder, get_redis_holder, get_request_id
from app.domain.enums import ManagerType
from app.domain.models.audit_event import Actor, EventType
from app.domain.ports.regex_engine import RegexEngine
from app.domain.services.classification import ClassifiableServer
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.services.search import build_filter_query, resolve_sort_field
from app.domain.value_objects.nic_names import nic_name_catalog
from app.errors import NotFoundError, PageSizeTooLargeError, ValidationAppError
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.cache import (
    FACETS_TTL_SECONDS,
    LIST_PAGE_TTL_SECONDS,
    SERVER_DETAIL_TTL_SECONDS,
    CacheClient,
)
from app.infrastructure.redis.client import RedisClientHolder
from app.infrastructure.redis.keys import (
    facets_key,
    list_and_facets_patterns,
    list_key,
    server_key,
)
from app.infrastructure.singleflight import coalesce
from app.utils.digest import stable_hash
from app.utils.timeutil import utcnow

router = APIRouter(prefix="/api/v1", tags=["servers"])

# Query params handled explicitly by `list_servers`; everything else in
# the query string is a candidate filter key (see module docstring).
_NON_FILTER_PARAMS = frozenset({"search", "sort", "sort_desc", "cursor", "page_size", "with_count"})

_TRUE_STRINGS = frozenset({"true", "1", "yes"})
_FALSE_STRINGS = frozenset({"false", "0", "no"})


def _parse_bool(raw: str, *, field: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE_STRINGS:
        return True
    if lowered in _FALSE_STRINGS:
        return False
    raise ValidationAppError(
        f"Query parameter {field!r} must be a boolean.", details={"field": field, "value": raw}
    )


def _extract_raw_filters(request: Request) -> dict[str, object]:
    filters: dict[str, object] = {}
    for key, value in request.query_params.items():
        if key in _NON_FILTER_PARAMS:
            continue
        if key == "maintenance":
            filters[key] = _parse_bool(value, field="maintenance")
        else:
            filters[key] = value
    return filters


async def _server_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MongoServerRepository:
    """
    Build the server repository for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.
        settings (Settings): Supplies the cursor-signing secret.

    Returns:
        MongoServerRepository: A repository bound to that client.
    """
    return MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)


async def _cache_client(
    redis: Annotated[RedisClientHolder, Depends(get_redis_holder)],
) -> CacheClient:
    """
    Build the cache-aside client for one request.

    Args:
        redis (RedisClientHolder): The shared Redis client holder.

    Returns:
        CacheClient: A cache client bound to that connection.
    """
    return CacheClient(redis)


_METRIC_REGISTRY = build_default_registry()


async def _regex_engine(settings: Annotated[Settings, Depends(get_settings)]) -> RegexEngine:
    """
    Build the regex engine used to evaluate classification rules.

    Args:
        settings (Settings): Supplies the pattern-length and timeout limits.

    Returns:
        RegexEngine: The configured engine.
    """
    return RegexModuleEngine(
        max_pattern_length=settings.regex_max_pattern_length,
        match_timeout_seconds=settings.regex_match_timeout_seconds,
    )


async def _classification_service(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    engine: Annotated[RegexEngine, Depends(_regex_engine)],
) -> ClassificationService:
    """
    Build the classification service for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.
        engine (RegexEngine): The regex engine to evaluate rules with.

    Returns:
        ClassificationService: A service bound to those dependencies.
    """
    return ClassificationService(rule_repo=MongoClassificationRuleRepository(mongo), engine=engine)


async def _health_policy_service(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> HealthPolicyService:
    """
    Build the health policy service for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.

    Returns:
        HealthPolicyService: A service bound to that client and the
            module-level metric registry.
    """
    return HealthPolicyService(
        policy_repo=MongoHealthPolicyRepository(mongo),
        registry=_METRIC_REGISTRY,
    )


async def _audit_service(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> AuditService:
    """
    Build the audit service for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.

    Returns:
        AuditService: A service bound to that client.
    """
    return AuditService(repo=MongoAuditEventRepository(mongo))


async def _maintenance_service(
    server_repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    audit: Annotated[AuditService, Depends(_audit_service)],
) -> MaintenanceService:
    """
    Build the maintenance service for one request.

    Args:
        server_repo (MongoServerRepository): The server repository.
        audit (AuditService): Records the maintenance-change audit event.

    Returns:
        MaintenanceService: A service bound to those dependencies.
    """
    return MaintenanceService(server_repo=server_repo, audit=audit)


def _revision_pointer_key(server_id: str) -> str:
    """Build the cache key mapping a server ID to its current revision.

    Lets `server_key(id, revision)` be looked up without a full Mongo read
    first. See the module docstring.

    Args:
        server_id (str): The server's ID.

    Returns:
        str: The pointer entry's cache key.
    """
    return f"si:1:srv:{server_id}:rev"


@router.get("/servers", response_model=ServerListResponse)
async def list_servers(
    request: Request,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    search: str | None = Query(default=None),
    sort: str = Query(default="name"),
    sort_desc: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    page_size: int | None = Query(default=None, ge=1),
    with_count: bool = Query(default=False),
) -> ServerListResponse | Response:
    """
    List servers, keyset-paginated, with generic filters and search.

    Every query parameter outside the fixed non-filter set (`search`,
    `sort`, `sort_desc`, `cursor`, `page_size`, `with_count`) is treated as
    a filter key and validated against the whitelist — see the module
    docstring for why that is generic rather than individually typed.

    Args:
        request (Request): Carries the raw filter query parameters.
        repo (MongoServerRepository): The server repository.
        cache (CacheClient): Cache-aside for the list page.
        settings (Settings): Supplies the default/max page size.
        search (str | None): Free-text search over the server's search tokens.
        sort (str): The field to sort by.
        sort_desc (bool): Sort descending instead of ascending.
        cursor (str | None): Opaque keyset cursor from a previous page.
        page_size (int | None): Page size; defaults to `settings.default_page_size`.
        with_count (bool): Whether to compute the total matching count.

    Returns:
        ServerListResponse | Response: The page of servers — a raw cached
            JSON body on a cache hit (see the module docstring), the
            validated model on a miss.

    Raises:
        UnknownFilterError: A query parameter isn't a recognized filter key.
        PageSizeTooLargeError: `page_size` exceeds `settings.max_page_size`.
    """
    effective_page_size = page_size if page_size is not None else settings.default_page_size
    if effective_page_size > settings.max_page_size:
        raise PageSizeTooLargeError(
            f"page_size must not exceed {settings.max_page_size}.",
            details={"max_page_size": settings.max_page_size, "page_size": effective_page_size},
        )

    raw_filters = _extract_raw_filters(request)
    # Validate eagerly (unknown filter / unknown sort) before touching the
    # cache or Mongo — a request that can never succeed shouldn't cost a
    # Redis round trip first.
    mongo_filters = build_filter_query(raw_filters)
    resolve_sort_field(sort)  # fail fast on an unknown sort before any I/O

    cache_key = list_key(
        stable_hash(
            {
                "filters": mongo_filters,
                "search": search,
                "sort": sort,
                "sort_desc": sort_desc,
                "page_size": effective_page_size,
                "with_count": with_count,
            }
        ),
        stable_hash({"cursor": cursor}),
    )

    cached = await cache.get_raw(cache_key)
    if cached is not None:
        # The bytes in Redis are already the bytes on the wire — `set()`
        # below caches `response.model_dump(mode="json")`, JSON-encoded,
        # which is exactly what FastAPI would re-encode a validated
        # `ServerListResponse` back into. Returning them directly skips a
        # decode + re-validate + re-encode round trip that measured at
        # 0.919 ms/request and changed nothing about the bytes on the wire
        # (`docs/notes/2026-09-research-performance.md` §7.2, §6.1).
        return Response(content=cached, media_type="application/json")

    # Coalesced, not just cached: concurrent identical requests that all
    # arrive before the first one has written its result back (the same
    # `cache_key`, within `LIST_PAGE_TTL_SECONDS`) share a single Mongo
    # query instead of each re-running it — see
    # `app.infrastructure.singleflight`'s docstring for the load-test
    # finding (a low-selectivity search's p99 went from tens of ms to
    # multi-second under concurrent load) that motivated this.
    async def _compute() -> dict[str, object]:
        page = await repo.list_page(
            filters=mongo_filters,
            search=search,
            sort=sort,
            sort_desc=sort_desc,
            cursor=cursor,
            page_size=effective_page_size,
            with_count=with_count,
        )
        response = ServerListResponse(
            items=[ServerSummary.from_server(server) for server in page.items],
            page=PageInfo(
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                page_size=effective_page_size,
                count=page.total_count,
                count_capped=False,
            ),
        )
        dumped = response.model_dump(mode="json")
        await cache.set(cache_key, dumped, ttl_seconds=LIST_PAGE_TTL_SECONDS)
        return dumped

    response_dict = await coalesce(cache_key, _compute)
    return ServerListResponse.model_validate(response_dict)


# Declared BEFORE `/servers/{server_id}`: FastAPI matches routes in
# declaration order, and the other way round "facets" is swallowed as a
# server id and answers 404.
@router.get("/servers/facets", response_model=ServerFacets)
async def server_facets(
    request: Request,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    search: str | None = Query(default=None),
) -> ServerFacets | Response:
    """
    How many servers each filter option would match, under the current filters.

    Takes the same `?vendor=`/`?site_id=`/... parameters and the same
    `?search=` as `GET /servers`, so a caller passes its current query
    through unchanged and gets numbers describing exactly the page it is
    showing.

    Args:
        request (Request): Carries the raw filter query parameters.
        repo (MongoServerRepository): The server repository.
        cache (CacheClient): Cache-aside for the aggregation.
        search (str | None): The same free-text search `GET /servers` takes.

    Returns:
        ServerFacets | Response: One count per option, plus the matching
            total — a raw cached JSON body on a cache hit (see
            `list_servers`'s docstring for why), the validated model on a
            miss.
    """
    raw_filters = _extract_raw_filters(request)
    mongo_filters = build_filter_query(raw_filters)

    cache_key = facets_key(stable_hash({"filters": mongo_filters, "search": search}))
    cached = await cache.get_raw(cache_key)
    if cached is not None:
        return Response(content=cached, media_type="application/json")

    facets = ServerFacets.from_rows(
        await repo.facet_breakdown(filters=mongo_filters, search=search)
    )
    await cache.set(cache_key, facets.model_dump(mode="json"), ttl_seconds=FACETS_TTL_SECONDS)
    return facets


@router.get("/servers/{server_id}", response_model=ServerDetail)
async def get_server(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ServerDetail | Response:
    """
    Get one server's full detail.

    Args:
        server_id (str): The server's ID.
        repo (MongoServerRepository): The server repository.
        cache (CacheClient): Cache-aside for the detail document, keyed by
            revision (see the module docstring).
        settings (Settings): Supplies the NIC OS-name mapping.

    Returns:
        ServerDetail | Response: The server's detail — a raw cached JSON
            body on a cache hit (see `list_servers`'s docstring for why),
            the validated model on a miss.

    Raises:
        NotFoundError: No server has that ID.
    """
    pointer_key = _revision_pointer_key(server_id)
    cached_revision = await cache.get(pointer_key)
    if isinstance(cached_revision, int):
        # The pointer itself is a bare int, not a document — still decoded
        # normally via `get`. Only the detail document behind it is large
        # enough to skip the decode/re-validate/re-encode round trip for
        # (see `list_servers`'s docstring for the measurement).
        cached_detail = await cache.get_raw(server_key(server_id, cached_revision))
        if cached_detail is not None:
            return Response(content=cached_detail, media_type="application/json")

    server = await repo.get_by_id(server_id)
    if server is None:
        raise NotFoundError(f"No server with id {server_id!r}.", details={"server_id": server_id})

    detail = ServerDetail.from_server(server, nic_name_catalog(settings.nic_os_names))
    detail_key = server_key(server_id, server.revision)
    await cache.set(pointer_key, server.revision, ttl_seconds=SERVER_DETAIL_TTL_SECONDS)
    await cache.set(
        detail_key, detail.model_dump(mode="json"), ttl_seconds=SERVER_DETAIL_TTL_SECONDS
    )
    return detail


async def _invalidate_detail_cache(server_id: str, cache: CacheClient) -> None:
    """Delete the revision-pointer cache entry so the next read sees the new revision.

    `server_key` embeds `revision`, so bumping `revision` on write already
    makes the previous cache entry unreachable — but the pointer entry
    (`_revision_pointer_key`) still points at the old revision until it
    expires on its own TTL, which would cost one extra (harmless, but
    avoidable) Mongo round trip on the very next `GET`.

    Args:
        server_id (str): The server whose pointer entry to delete.
        cache (CacheClient): The cache client.
    """
    await cache.delete(_revision_pointer_key(server_id))


async def _invalidate_list_cache(cache: CacheClient) -> None:
    """Drop every cached list page and facet count after an operator write.

    The two maintenance endpoints only — never ingest. ADR-0028 says why.

    Args:
        cache (CacheClient): The cache client.
    """
    await cache.delete_matching(*list_and_facets_patterns())


@router.post("/servers/{server_id}/reclassify", response_model=ServerDetail)
async def reclassify_server(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    service: Annotated[ClassificationService, Depends(_classification_service)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ServerDetail:
    """
    Re-run classification for one server against the current ruleset.

    The same classification step ingestion runs automatically, exposed
    here so editing a rule can be followed by "show me the effect on this
    server" without waiting for the server's next ingest cycle. Persists
    the result and records a `CLASSIFICATION_CHANGED` audit event when the
    installation type changes.

    Args:
        server_id (str): The server's ID.
        repo (MongoServerRepository): The server repository.
        cache (CacheClient): Cache-aside to invalidate on write.
        service (ClassificationService): Runs the classification engine.
        audit (AuditService): Records the change, if any.
        actor (Actor): The actor to attribute the audit event to.
        request_id (str | None): The current request's ID, for the audit event.
        settings (Settings): Supplies the NIC OS-name mapping.

    Returns:
        ServerDetail: The server after reclassification.

    Raises:
        NotFoundError: No server has that ID.
    """
    server = await repo.get_by_id(server_id)
    if server is None:
        raise NotFoundError(f"No server with id {server_id!r}.", details={"server_id": server_id})

    expected_revision = server.revision
    previous_type = server.classification.installation_type
    classifiable = ClassifiableServer(
        name=server.name,
        vendor=server.identity.vendor,
        manager_type=ManagerType(server.source_provider) if server.source_provider else None,
        site_id=server.site_id,
        serial=server.identity.serial,
        model=server.model,
    )
    result = await service.classify_server(classifiable)
    server.classification = classification_from_result(
        result, previous_version=server.classification.classification_version
    )
    server.revision += 1
    server.updated_at = utcnow()

    await repo.upsert_with_revision_check(server, expected_revision=expected_revision)
    await _invalidate_detail_cache(server_id, cache)

    if server.classification.installation_type != previous_type:
        await audit.record(
            EventType.CLASSIFICATION_CHANGED,
            actor=actor,
            server_id=server_id,
            request_id=request_id,
            data={
                "from": previous_type.value,
                "to": server.classification.installation_type.value,
                "matched_rule_id": server.classification.matched_rule_id,
            },
        )
    return ServerDetail.from_server(server, nic_name_catalog(settings.nic_os_names))


@router.post("/servers/{server_id}/health/recalculate", response_model=ServerDetail)
async def recalculate_server_health(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    service: Annotated[HealthPolicyService, Depends(_health_policy_service)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ServerDetail:
    """
    Re-run health evaluation for one server against the current policy set.

    Same rationale as `reclassify_server`: proves "I edited a threshold,
    did this server's health change" without waiting for its next ingest.
    Persists the result and records a `HEALTH_STATUS_CHANGED` audit event
    when the overall severity changes.

    Args:
        server_id (str): The server's ID.
        repo (MongoServerRepository): The server repository.
        cache (CacheClient): Cache-aside to invalidate on write.
        service (HealthPolicyService): Runs the health policy engine.
        audit (AuditService): Records the change, if any.
        actor (Actor): The actor to attribute the audit event to.
        request_id (str | None): The current request's ID, for the audit event.
        settings (Settings): Supplies the NIC OS-name mapping.

    Returns:
        ServerDetail: The server after health re-evaluation.

    Raises:
        NotFoundError: No server has that ID.
    """
    server = await repo.get_by_id(server_id)
    if server is None:
        raise NotFoundError(f"No server with id {server_id!r}.", details={"server_id": server_id})

    expected_revision = server.revision
    previous_overall = server.health.overall
    state = await service.evaluate_server(server)
    server.health = health_from_state(state)
    server.revision += 1
    server.updated_at = utcnow()

    await repo.upsert_with_revision_check(server, expected_revision=expected_revision)
    await _invalidate_detail_cache(server_id, cache)

    if server.health.overall != previous_overall:
        await audit.record(
            EventType.HEALTH_STATUS_CHANGED,
            actor=actor,
            server_id=server_id,
            request_id=request_id,
            data={
                "from": previous_overall.value,
                "to": server.health.overall.value,
                "policy_ids": [e.policy_id for e in state.evaluations if e.active],
            },
        )
    return ServerDetail.from_server(server, nic_name_catalog(settings.nic_os_names))


@router.put("/servers/{server_id}/maintenance", response_model=ServerDetail)
async def enable_maintenance(
    server_id: str,
    payload: MaintenanceEnableRequest,
    service: Annotated[MaintenanceService, Depends(_maintenance_service)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ServerDetail:
    """
    Enable maintenance mode on a server.

    Args:
        server_id (str): The server's ID.
        payload (MaintenanceEnableRequest): The reason/ticket/expected end.
        service (MaintenanceService): Applies the maintenance state change.
        cache (CacheClient): Cache-aside to invalidate on write.
        actor (Actor): The actor to attribute the audit event to.
        request_id (str | None): The current request's ID, for the audit event.
        settings (Settings): Supplies the NIC OS-name mapping.

    Returns:
        ServerDetail: The server with maintenance enabled.

    Raises:
        NotFoundError: No server has that ID.
    """
    server = await service.enable(
        server_id,
        reason=payload.reason,
        ticket=payload.ticket,
        expected_end=payload.expected_end,
        actor=actor,
        request_id=request_id,
    )
    await _invalidate_detail_cache(server_id, cache)
    await _invalidate_list_cache(cache)
    return ServerDetail.from_server(server, nic_name_catalog(settings.nic_os_names))


@router.delete("/servers/{server_id}/maintenance", response_model=ServerDetail)
async def disable_maintenance(
    server_id: str,
    service: Annotated[MaintenanceService, Depends(_maintenance_service)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ServerDetail:
    """
    Disable maintenance mode on a server.

    Args:
        server_id (str): The server's ID.
        service (MaintenanceService): Applies the maintenance state change.
        cache (CacheClient): Cache-aside to invalidate on write.
        actor (Actor): The actor to attribute the audit event to.
        request_id (str | None): The current request's ID, for the audit event.
        settings (Settings): Supplies the NIC OS-name mapping.

    Returns:
        ServerDetail: The server with maintenance disabled.

    Raises:
        NotFoundError: No server has that ID.
    """
    server = await service.disable(server_id, actor=actor, request_id=request_id)
    await _invalidate_detail_cache(server_id, cache)
    await _invalidate_list_cache(cache)
    return ServerDetail.from_server(server, nic_name_catalog(settings.nic_os_names))
