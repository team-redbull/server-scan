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

from datetime import datetime, timedelta
from hashlib import blake2b
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.v1.maintenance_schemas import MaintenanceEnableRequest
from app.api.v1.schemas import (
    AvailableServerItem,
    AvailableServersResponse,
    PageInfo,
    ServerDetail,
    ServerListResponse,
    ServerRowsResponse,
    ServerSummary,
)
from app.application.services.audit_service import AuditService
from app.application.services.available_servers import (
    SELECTABLE_TIERS,
    AmbiguousServerNameError,
    AvailableServersNotFoundError,
    AvailableServersService,
)
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService
from app.application.services.maintenance_service import MaintenanceService
from app.application.services.pipeline import classification_from_result, health_from_state
from app.config import Settings, get_settings
from app.dependencies import (
    get_mongo_holder,
    get_redis_holder,
    get_request_id,
    require_admin,
)
from app.domain.enums import HealthSeverity, ManagerType, Vendor
from app.domain.models.audit_event import Actor, EventType
from app.domain.ports.regex_engine import RegexEngine
from app.domain.services.classification import ClassifiableServer
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.services.search import build_filter_query, resolve_sort_field
from app.domain.value_objects.capacity_aliases import capacity_alias_catalog
from app.domain.value_objects.gpu_catalog import gpu_catalog
from app.domain.value_objects.nic_names import nic_name_catalog
from app.domain.value_objects.site import site_catalog
from app.errors import (
    AvailableCountTooLargeError,
    AvailableLookupConflictingParamsError,
    AvailableServerNameAmbiguousError,
    AvailableServerNotFoundError,
    NotFoundError,
    PageSizeTooLargeError,
    ValidationAppError,
)
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.factory import build_provider_for_manager_type
from app.infrastructure.redis.cache import (
    LIST_PAGE_TTL_SECONDS,
    SERVER_DETAIL_TTL_SECONDS,
    CacheClient,
)
from app.infrastructure.redis.client import RedisClientHolder
from app.infrastructure.redis.keys import (
    list_cache_patterns,
    list_key,
    rows_key,
    server_key,
)
from app.infrastructure.singleflight import coalesce
from app.utils.digest import stable_hash
from app.utils.timeutil import utcnow

router = APIRouter(prefix="/api/v1", tags=["servers"])

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
        if key in ("maintenance", "stale"):
            filters[key] = _parse_bool(value, field=key)
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


async def _ingest_service(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
    server_repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    classification_service: Annotated[ClassificationService, Depends(_classification_service)],
    health_service: Annotated[HealthPolicyService, Depends(_health_policy_service)],
    audit: Annotated[AuditService, Depends(_audit_service)],
) -> IngestService:
    """
    Build the ingest pipeline `GET /servers/available`'s live recheck writes through.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.
        settings (Settings): Supplies the site and GPU catalogs.
        server_repo (MongoServerRepository): The server repository.
        classification_service (ClassificationService): Classifies a
            freshly rechecked server.
        health_service (HealthPolicyService): Health-evaluates it.
        audit (AuditService): Records any transition a recheck causes.

    Returns:
        IngestService: A service bound to those dependencies.
    """
    return IngestService(
        server_repo=server_repo,
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
        sites=site_catalog(settings.sites),
        gpu_catalog=gpu_catalog(settings.gpu_models),
        classification_service=classification_service,
        health_service=health_service,
        audit=audit,
    )


async def _available_servers_service(
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    ingest_service: Annotated[IngestService, Depends(_ingest_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AvailableServersService:
    """
    Build the `GET /servers/available` resolver for one request.

    Args:
        repo (MongoServerRepository): The server repository.
        ingest_service (IngestService): Runs a live recheck's fetched
            record through the ingest pipeline.
        settings (Settings): Supplies the capacity-alias catalog and
            resolves each manager type's provider.

    Returns:
        AvailableServersService: A service bound to those dependencies.
    """
    return AvailableServersService(
        repo=repo,
        ingest=ingest_service,
        provider_factory=lambda manager_type: build_provider_for_manager_type(
            manager_type, settings=settings
        ),
        capacity_aliases=capacity_alias_catalog(settings.capacity_aliases),
    )


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


def _stale_before(settings: Settings) -> datetime:
    """
    The staleness cutoff on the API's clock, for the `stale` flag on responses.

    ADR-0029's window; the *filter* uses Mongo's clock (`stale_cutoff_expr`).

    Args:
        settings (Settings): Supplies `stale_after_seconds`.

    Returns:
        datetime: `now - INVENTORY_STALE_AFTER_SECONDS`.
    """
    return utcnow() - timedelta(seconds=settings.stale_after_seconds)


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

    Every query parameter outside the fixed non-filter set is a filter key
    validated against the whitelist — see the module docstring.

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
    # Validated before any I/O: a doomed request should not cost a Redis trip.
    mongo_filters = build_filter_query(
        raw_filters, stale_after_seconds=settings.stale_after_seconds
    )
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
        # Cached bytes are the wire bytes; skipping the decode/re-encode
        # round trip measured 0.919 ms/request (architecture.md, "caching").
        return Response(content=cached, media_type="application/json")

    # Coalesced, not just cached: concurrent identical misses share one
    # Mongo query (ADR-0007, `app.infrastructure.singleflight`).
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
            items=[
                ServerSummary.from_server(server, stale_before=_stale_before(settings))
                for server in page.items
            ],
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


def _weak_etag(body: bytes) -> str:
    """
    A weak validator for a response body (RFC 9110 §8.8.3).

    Weak, because the gzip middleware changes the bytes on the wire but
    not the representation.

    Args:
        body (bytes): The JSON body.

    Returns:
        str: The `ETag` header value.
    """
    return f'W/"{blake2b(body, digest_size=16).hexdigest()}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """
    Whether an `If-None-Match` header names this ETag (weak comparison).

    Args:
        if_none_match (str | None): The request header, if any.
        etag (str): The response's own ETag.

    Returns:
        bool: True when the client's copy is current.
    """
    if not if_none_match:
        return False
    wanted = etag.removeprefix("W/")
    return if_none_match.strip() == "*" or any(
        tag.strip().removeprefix("W/") == wanted for tag in if_none_match.split(",")
    )


@router.get("/servers/rows", response_model=ServerRowsResponse)
async def server_rows(
    request: Request,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """
    The whole fleet as flat inventory rows, for client-side filtering (ADR-0033).

    Cached as wire bytes, cleared with the list pages (ADR-0028), and
    weak-ETagged so a poller whose copy is current gets a bodiless 304.

    Args:
        request (Request): Carries `If-None-Match`.
        repo (MongoServerRepository): The server repository.
        cache (CacheClient): Cache-aside for the body.
        settings (Settings): Supplies the staleness window.

    Returns:
        Response: The JSON body with `ETag` and `Cache-Control: no-cache`,
            or a 304 when the client's ETag is current.
    """
    key = rows_key()
    cached = await cache.get_raw(key)
    body: bytes
    if cached is not None:
        body = cached if isinstance(cached, bytes) else cached.encode()
    else:

        async def _compute() -> bytes:
            response = ServerRowsResponse.from_docs(
                await repo.list_rows(), stale_before=_stale_before(settings)
            )
            encoded = response.model_dump_json().encode()
            await cache.set_raw(key, encoded, ttl_seconds=LIST_PAGE_TTL_SECONDS)
            return encoded

        body = await coalesce(key, _compute)

    etag = _weak_etag(body)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


# Before `/servers/{server_id}`: FastAPI matches in declaration order, and
# `available` would otherwise be swallowed as a `server_id` path value.
@router.get("/servers/available", response_model=AvailableServersResponse)
async def available_servers(
    service: Annotated[AvailableServersService, Depends(_available_servers_service)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    name: str | None = Query(default=None),
    pattern: str | None = Query(default=None),
    count: int | None = Query(default=None, ge=1),
    vendor: Vendor | None = Query(default=None),
    source_provider: ManagerType | None = Query(default=None),
    health: HealthSeverity | None = Query(default=None),
    min_nic_macs: int = Query(default=0, ge=0, le=16),
) -> AvailableServersResponse:
    """
    Find one or more assignable, live-verified servers for a BMH-creation caller.

    Exactly one of `name`/`pattern` selects the lookup mode — see ADR-0032.

    Args:
        service (AvailableServersService): Resolves both lookup modes.
        cache (CacheClient): Invalidated for every server a live recheck writes.
        settings (Settings): Supplies `max_available_count` and the NIC
            OS-name mapping for each item's `interfaces[].os_name`.
        name (str | None): Exact, case-insensitive server name.
        pattern (str | None): A MongoDB regex against `Server.name`.
        count (int | None): Pattern mode only; how many servers to return.
        vendor (Vendor | None): Restrict to one vendor.
        source_provider (ManagerType | None): Restrict to one collector.
        health (HealthSeverity | None): Restrict to exactly this tier instead
            of filling `HEALTHY` then `WARNING` then `MAJOR`. A caller that
            only provisions on `HEALTHY` hardware would otherwise be handed —
            and pay a live recheck for — a `WARNING` server whenever no
            `HEALTHY` one is free.
        min_nic_macs (int): Require this many NIC MACs, read this collection
            run. A BMH caller should pass at least 1 — a server with none
            cannot become a `BareMetalHost`, which has no `bootMACAddress`
            without one — and 2 for a bonded NMState configuration. Opt-in
            (default 0) so the endpoint's existing behaviour is unchanged.

    Returns:
        AvailableServersResponse: Always list-shaped, `name` mode returning
            at most one item.

    Raises:
        AvailableLookupConflictingParamsError: Neither or both of `name`/
            `pattern` were given, or `count` was given with `name`.
        AvailableCountTooLargeError: `count` exceeds `settings.max_available_count`.
        AvailableServerNameAmbiguousError: `name` matched several servers.
        AvailableServerNotFoundError: Nothing could be returned at all —
            see `AvailableServersNotFoundError`'s reason in the detail.
    """
    if (name is None) == (pattern is None):
        raise AvailableLookupConflictingParamsError(
            "Exactly one of `name` or `pattern` must be given."
        )
    if name is not None and count is not None:
        raise AvailableLookupConflictingParamsError(
            "`count` only applies to `pattern` mode, since `name` always resolves to at "
            "most one server."
        )

    extra_filters = build_filter_query(
        {
            key: value.value
            for key, value in (("vendor", vendor), ("source_provider", source_provider))
            if value is not None
        }
    )
    nic_names = nic_name_catalog(settings.nic_os_names)
    if health is not None and health not in SELECTABLE_TIERS:
        raise AvailableLookupConflictingParamsError(
            "health must be one of "
            f"{', '.join(tier.value for tier in SELECTABLE_TIERS)}; "
            f"{health.value} is never assignable."
        )
    # One tier when the caller named one, otherwise the full best-first fill.
    tiers = (health,) if health is not None else SELECTABLE_TIERS

    try:
        if name is not None:
            result = await service.lookup_by_name(
                name,
                extra_filters=extra_filters,
                tiers=tiers,
                min_nic_macs=min_nic_macs,
            )
            results = [result]
            mode, requested = "name", 1
        else:
            effective_count = count if count is not None else 1
            if effective_count > settings.max_available_count:
                raise AvailableCountTooLargeError(
                    f"count must not exceed {settings.max_available_count}.",
                    details={
                        "max_available_count": settings.max_available_count,
                        "count": effective_count,
                    },
                )
            assert pattern is not None  # narrowed by the xor check above
            outcome = await service.lookup_by_pattern(
                pattern,
                count=effective_count,
                extra_filters=extra_filters,
                tiers=tiers,
                min_nic_macs=min_nic_macs,
            )
            results = outcome.items
            mode, requested = "pattern", outcome.requested
    except AmbiguousServerNameError as exc:
        raise AvailableServerNameAmbiguousError(
            f"{exc.name!r} matches {len(exc.server_ids)} servers. Server names are "
            "not unique — correlation is on (vendor, serial). Narrow the lookup "
            "with `vendor`/`source_provider`.",
            details={"name": exc.name, "server_ids": exc.server_ids},
        ) from exc
    except AvailableServersNotFoundError as exc:
        raise AvailableServerNotFoundError(exc.reason) from exc

    for result in results:
        await _invalidate_detail_cache(result.server.id, cache)
    if results:
        await _invalidate_list_cache(cache)

    return AvailableServersResponse(
        items=[
            AvailableServerItem.from_server(
                result.server,
                nic_names=nic_names,
                live_recheck_performed=result.live_recheck_performed,
            )
            for result in results
        ],
        mode=mode,
        requested=requested,
        returned=len(results),
    )


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
        cached_detail = await cache.get_raw(server_key(server_id, cached_revision))
        if cached_detail is not None:
            return Response(content=cached_detail, media_type="application/json")

    server = await repo.get_by_id(server_id)
    if server is None:
        raise NotFoundError(f"No server with id {server_id!r}.", details={"server_id": server_id})

    detail = ServerDetail.from_server(
        server, nic_name_catalog(settings.nic_os_names), stale_before=_stale_before(settings)
    )
    detail_key = server_key(server_id, server.revision)
    await cache.set(pointer_key, server.revision, ttl_seconds=SERVER_DETAIL_TTL_SECONDS)
    await cache.set(
        detail_key, detail.model_dump(mode="json"), ttl_seconds=SERVER_DETAIL_TTL_SECONDS
    )
    return detail


async def _invalidate_detail_cache(server_id: str, cache: CacheClient) -> None:
    """Delete the revision-pointer cache entry so the next read sees the new revision.

    The detail entry is already unreachable (its key embeds `revision`);
    only the pointer would otherwise linger until its TTL.

    Args:
        server_id (str): The server whose pointer entry to delete.
        cache (CacheClient): The cache client.
    """
    await cache.delete(_revision_pointer_key(server_id))


async def _invalidate_list_cache(cache: CacheClient) -> None:
    """Drop every cached list page and the rows body after an operator write.

    The two maintenance endpoints only — never ingest. ADR-0028 says why.

    Args:
        cache (CacheClient): The cache client.
    """
    await cache.delete_matching(*list_cache_patterns())


@router.post("/servers/{server_id}/reclassify", response_model=ServerDetail)
async def reclassify_server(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    service: Annotated[ClassificationService, Depends(_classification_service)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(require_admin)],
    request_id: Annotated[str | None, Depends(get_request_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ServerDetail:
    """
    Re-run classification for one server against the current ruleset.

    The same step ingestion runs, on demand. Records a
    `CLASSIFICATION_CHANGED` audit event when the installation type changes.

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
    return ServerDetail.from_server(
        server, nic_name_catalog(settings.nic_os_names), stale_before=_stale_before(settings)
    )


@router.post("/servers/{server_id}/health/recalculate", response_model=ServerDetail)
async def recalculate_server_health(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    service: Annotated[HealthPolicyService, Depends(_health_policy_service)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(require_admin)],
    request_id: Annotated[str | None, Depends(get_request_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ServerDetail:
    """
    Re-run health evaluation for one server against the current policy set.

    Same rationale as `reclassify_server`. Records a `HEALTH_STATUS_CHANGED`
    audit event when the overall severity changes.

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
    return ServerDetail.from_server(
        server, nic_name_catalog(settings.nic_os_names), stale_before=_stale_before(settings)
    )


@router.put("/servers/{server_id}/maintenance", response_model=ServerDetail)
async def enable_maintenance(
    server_id: str,
    payload: MaintenanceEnableRequest,
    service: Annotated[MaintenanceService, Depends(_maintenance_service)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    actor: Annotated[Actor, Depends(require_admin)],
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
    return ServerDetail.from_server(
        server, nic_name_catalog(settings.nic_os_names), stale_before=_stale_before(settings)
    )


@router.delete("/servers/{server_id}/maintenance", response_model=ServerDetail)
async def disable_maintenance(
    server_id: str,
    service: Annotated[MaintenanceService, Depends(_maintenance_service)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    actor: Annotated[Actor, Depends(require_admin)],
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
    return ServerDetail.from_server(
        server, nic_name_catalog(settings.nic_os_names), stale_before=_stale_before(settings)
    )
