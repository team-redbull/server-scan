"""
API tests for `GET /api/v1/servers` and `GET /api/v1/servers/{id}` against a
real app and the live dev Mongo + Redis. Data is inserted through
`MongoServerRepository`, never ingestion, to pin the HTTP/query/cache contract.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.domain.enums import HealthSeverity, InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.network import BmcInfo, NetworkInfo, NetworkInterface
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.client import RedisClientHolder
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


def _make_server(
    index: int,
    *,
    site_id: str | None = None,
    vendor: Vendor = Vendor.DELL,
    health: HealthSeverity = HealthSeverity.UNKNOWN,
    installation_type: InstallationType = InstallationType.UNCLASSIFIED,
    maintenance_enabled: bool = False,
    name: str | None = None,
    bmc_host: str | None = None,
    interfaces: tuple[NetworkInterface, ...] = (),
) -> Server:
    now = utcnow()
    nm = name if name is not None else f"api-test-srv-{index:04d}"
    serial = f"APITEST{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=nm,
        name_normalized=normalize_text(nm),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"api-test-uuid-{index:06d}",
        ),
        site_id=site_id,
        classification=Classification(installation_type=installation_type),
        health=Health(overall=health),
        maintenance=Maintenance(enabled=maintenance_enabled),
        network=NetworkInfo(bmc=BmcInfo(host=bmc_host), interfaces=list(interfaces)),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


@pytest.fixture
async def app_context() -> AsyncIterator[tuple[AsyncClient, MongoServerRepository]]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        redis: RedisClientHolder = app.state.redis
        for name in ("servers", "sites", "managers"):
            await mongo.db[name].delete_many({})
        # Tests reuse the same filter/sort combinations, so flush the list cache
        # between them. Best-effort: cache tests don't require Redis to be up.
        with contextlib.suppress(Exception):
            await redis.client.flushdb()

        repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        yield client, repo

        for name in ("servers", "sites", "managers"):
            await mongo.db[name].delete_many({})


async def test_list_returns_expected_items(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    for i in range(3):
        await repo.upsert(_make_server(i))

    resp = await client.get("/api/v1/servers")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["page"]["has_more"] is False
    item = body["items"][0]
    assert set(item) == {
        "id",
        "name",
        "vendor",
        "model",
        "site_id",
        "manager_id",
        "source_provider",
        "classification",
        "health",
        "maintenance",
        "openshift",
        "connectivity",
        "last_seen_at",
        "stale",
        "reachable",
        "unreachable_since",
        "updated_at",
    }


async def test_list_excludes_hardware_from_summary(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_make_server(0))

    resp = await client.get("/api/v1/servers")

    assert resp.status_code == 200
    assert "hardware" not in resp.json()["items"][0]


async def test_search_matches_by_token(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_make_server(1, name="ocp-dell-worker-777"))
    await repo.upsert(_make_server(2, name="upi-cisco-master-778"))

    resp = await client.get("/api/v1/servers", params={"search": "ocp"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "ocp-dell-worker-777"


async def test_search_matches_by_bmc_host(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """`build_search_tokens` indexes `network.bmc.host` — the value `NetworkTab.tsx`
    shows as "Address", so search matches what an operator copies off the page.
    """
    client, repo = app_context
    await repo.upsert(_make_server(1, name="srv-with-bmc", bmc_host="10.20.30.41"))
    await repo.upsert(_make_server(2, name="srv-without-bmc"))

    resp = await client.get("/api/v1/servers", params={"search": "10.20.30"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "srv-with-bmc"


async def test_filter_by_site_id(app_context: tuple[AsyncClient, MongoServerRepository]) -> None:
    client, repo = app_context
    for i in range(3):
        await repo.upsert(_make_server(i, site_id="tlv"))
    for i in range(3, 5):
        await repo.upsert(_make_server(i, site_id="nyc"))

    resp = await client.get("/api/v1/servers", params={"site_id": "tlv"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert all(item["site_id"] == "tlv" for item in body["items"])


async def test_filter_by_unassigned_site(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """`?site_id=unassigned` lists the servers whose site is stored as null;
    without `build_filter_query`'s translation the Unassigned card linked to
    an always-empty list.
    """
    client, repo = app_context
    for i in range(3):
        await repo.upsert(_make_server(i, site_id="tlv"))
    for i in range(3, 5):
        await repo.upsert(_make_server(i, site_id=None))

    resp = await client.get("/api/v1/servers", params={"site_id": "unassigned"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert all(item["site_id"] is None for item in body["items"])


async def test_filter_by_maintenance_bool(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_make_server(1, maintenance_enabled=True))
    await repo.upsert(_make_server(2, maintenance_enabled=False))

    resp = await client.get("/api/v1/servers", params={"maintenance": "true"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["maintenance"]["enabled"] is True


async def test_page_size_too_large_returns_422(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context
    settings = get_settings()

    resp = await client.get("/api/v1/servers", params={"page_size": settings.max_page_size + 1})

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "PAGE_SIZE_TOO_LARGE"


async def test_unknown_filter_returns_400_problem_json(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers", params={"not_a_real_filter": "x"})

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "UNKNOWN_FILTER"
    assert body["status"] == 400
    assert "request_id" in body
    assert body["instance"] == "/api/v1/servers"
    # RFC 9457 names `type` and `title` as the two core members alongside
    # `status`/`detail`/`instance` — asserted here because nothing in the
    # suite previously did, so dropping either would pass every other test.
    assert body["type"] == "/problems/unknown-filter"
    assert body["title"] == "Unknown Filter"
    assert body["detail"] == "Unknown filter: 'not_a_real_filter'"


async def test_unknown_sort_returns_400(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers", params={"sort": "not_a_real_sort"})

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "UNKNOWN_SORT_FIELD"


async def test_cursor_round_trip_across_pages_no_duplicates(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    total = 7
    for i in range(total):
        await repo.upsert(_make_server(i))

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(total):
        params: dict[str, str] = {"page_size": "3"}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/servers", params=params)
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])
        if not body["page"]["has_more"]:
            break
        cursor = body["page"]["next_cursor"]
        assert cursor is not None

    assert len(seen) == total
    assert len(set(seen)) == total


async def test_stale_cursor_after_filter_change_returns_400(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    for i in range(5):
        await repo.upsert(_make_server(i, site_id="tlv"))
    for i in range(5, 8):
        await repo.upsert(_make_server(i, site_id="nyc"))

    first = await client.get("/api/v1/servers", params={"site_id": "tlv", "page_size": "2"})
    assert first.status_code == 200
    cursor = first.json()["page"]["next_cursor"]
    assert cursor is not None

    second = await client.get(
        "/api/v1/servers",
        params={"site_id": "nyc", "page_size": "2", "cursor": cursor},
    )

    assert second.status_code == 400
    assert second.json()["code"] == "CURSOR_FILTER_MISMATCH"


async def test_get_detail_200_for_existing_server(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    server = _make_server(1, name="detail-test-server")
    await repo.upsert(server)

    resp = await client.get(f"/api/v1/servers/{server.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == server.id
    assert body["name"] == "detail-test-server"
    assert "hardware" in body  # detail is a superset, unlike the list summary


async def test_get_detail_derives_cisco_eno_names(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    server = _make_server(
        1,
        name="cisco-eno-test-server",
        vendor=Vendor.CISCO,
        interfaces=(
            NetworkInterface(name="eth0", mac="00:11:22:33:44:00"),
            NetworkInterface(name="eth1", mac="00:11:22:33:44:01"),
        ),
    )
    await repo.upsert(server)

    resp = await client.get(f"/api/v1/servers/{server.id}")

    assert resp.status_code == 200
    assert resp.json()["nic_os_names"] == {"eth0": "eno5", "eth1": "eno6"}


async def test_get_detail_200_is_cache_stable_on_second_read(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    server = _make_server(1, name="detail-cache-test")
    await repo.upsert(server)

    first = await client.get(f"/api/v1/servers/{server.id}")
    second = await client.get(f"/api/v1/servers/{server.id}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    # The second read is the cached bytes verbatim (docs/notes/2026-09-audit.md
    # P2) and must still be a well-formed `application/json` response.
    assert second.headers["content-type"] == "application/json"


async def test_list_is_cache_stable_on_second_read(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Same P2 cache-hit path as the detail endpoint, for `GET /servers`:
    the second read returns the raw bytes `set()` cached on the first,
    never a `model_validate` + FastAPI re-encode round trip.
    """
    client, repo = app_context
    for i in range(3):
        await repo.upsert(_make_server(i, name=f"list-cache-test-{i}"))

    first = await client.get("/api/v1/servers")
    second = await client.get("/api/v1/servers")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert second.headers["content-type"] == "application/json"


async def test_facets_is_cache_stable_on_second_read(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    await repo.upsert(_make_server(1, name="facets-cache-test"))

    first = await client.get("/api/v1/servers/facets")
    second = await client.get("/api/v1/servers/facets")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert second.headers["content-type"] == "application/json"


async def test_get_detail_404_for_missing_server(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _repo = app_context

    resp = await client.get("/api/v1/servers/srv_does_not_exist")

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"


async def test_list_returns_200_from_mongo_when_redis_unreachable() -> None:
    """A Redis outage is a cache miss, never a request failure. Standalone (not
    `app_context`) so `app.state.redis` can be swapped for an unreachable port.
    """
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        for name in ("servers", "sites", "managers"):
            await mongo.db[name].delete_many({})
        repo = MongoServerRepository(mongo, cursor_secret=get_settings().cursor_secret)
        server = await repo.upsert(_make_server(1, name="redis-down-test-server"))

        broken_settings = Settings(
            redis_uri="redis://localhost:1/0",
            redis_connect_timeout_seconds=0.5,
            redis_socket_timeout_seconds=0.5,
        )
        broken_redis = RedisClientHolder(broken_settings)
        await broken_redis.connect()  # never raises, even unreachable
        app.state.redis = broken_redis

        try:
            resp = await client.get("/api/v1/servers")
            assert resp.status_code == 200
            body = resp.json()
            assert len(body["items"]) == 1
            assert body["items"][0]["name"] == "redis-down-test-server"

            detail_resp = await client.get(f"/api/v1/servers/{server.id}")
            assert detail_resp.status_code == 200
            assert detail_resp.json()["name"] == "redis-down-test-server"
        finally:
            await mongo.db["servers"].delete_many({})
            await broken_redis.close()


async def test_stale_flag_and_filter_follow_last_seen_at(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """`stale` is derived per response from `last_seen_at` (ADR-0029); `?stale=true`
    selects the same set Mongo-side, a never-seen server included, and the facet
    counts it.
    """
    client, repo = app_context
    settings = get_settings()
    window = timedelta(seconds=settings.stale_after_seconds)
    fresh = _make_server(0, name="stale-test-fresh")
    old = _make_server(1, name="stale-test-old")
    old.last_seen_at = utcnow() - window - timedelta(hours=1)
    never = _make_server(2, name="stale-test-never")
    never.last_seen_at = None
    for s in (fresh, old, never):
        await repo.upsert(s)

    listed = (await client.get("/api/v1/servers")).json()["items"]
    by_name = {s["name"]: s["stale"] for s in listed}
    assert by_name == {"stale-test-fresh": False, "stale-test-old": True, "stale-test-never": True}

    stale_only = (await client.get("/api/v1/servers", params={"stale": "true"})).json()
    assert sorted(s["name"] for s in stale_only["items"]) == ["stale-test-never", "stale-test-old"]

    fresh_only = (await client.get("/api/v1/servers", params={"stale": "false"})).json()
    assert [s["name"] for s in fresh_only["items"]] == ["stale-test-fresh"]

    facets = (await client.get("/api/v1/servers/facets")).json()
    assert facets["stale"] == {"true": 2, "false": 1}

    detail = (await client.get(f"/api/v1/servers/{old.id}")).json()
    assert detail["stale"] is True


async def test_stale_filter_pages_with_a_stable_cursor(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The stale clause carries no timestamp, so a cursor issued under `?stale=true`
    is still valid on the next request — the trap a rendered cutoff would hit.
    """
    client, repo = app_context
    settings = get_settings()
    for i in range(3):
        s = _make_server(i, name=f"stale-page-{i}")
        s.last_seen_at = utcnow() - timedelta(seconds=settings.stale_after_seconds + 3600)
        await repo.upsert(s)

    first = (await client.get("/api/v1/servers", params={"stale": "true", "page_size": 2})).json()
    assert len(first["items"]) == 2 and first["page"]["has_more"] is True

    second = await client.get(
        "/api/v1/servers",
        params={"stale": "true", "page_size": 2, "cursor": first["page"]["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1


async def test_list_is_gzipped_when_the_client_accepts_it(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """Gzipped on a cache miss and on the raw-bytes cache hit alike."""
    client, repo = app_context
    for i in range(20):
        await repo.upsert(_make_server(i))

    plain = await client.get("/api/v1/servers", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200
    assert "content-encoding" not in plain.headers

    for _ in range(2):
        resp = await client.get("/api/v1/servers", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        assert "Accept-Encoding" in resp.headers["vary"]
        assert resp.json() == plain.json()
        assert int(resp.headers["content-length"]) < len(plain.content) / 4


async def test_small_body_is_not_gzipped(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, _ = app_context

    resp = await client.get("/api/v1/servers", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


async def test_rows_returns_every_server_as_a_flat_row(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    seen = await repo.upsert(
        _make_server(
            1,
            site_id="tlv",
            vendor=Vendor.CISCO,
            health=HealthSeverity.CRITICAL,
            bmc_host="10.0.0.9",
            interfaces=(NetworkInterface(name="eno1", mac="aa:bb:cc:dd:ee:01"),),
        )
    )
    never = _make_server(2)
    never.last_seen_at = None
    await repo.upsert(never)

    resp = await client.get("/api/v1/servers/rows")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["etag"].startswith('W/"')
    body = resp.json()
    assert "generated_at" in body
    rows = {row["id"]: row for row in body["items"]}
    assert set(rows) == {seen.id, never.id}
    assert set(rows[seen.id]) == {
        "id",
        "name",
        "vendor",
        "model",
        "site_id",
        "source_provider",
        "installation_type",
        "health",
        "maintenance",
        "openshift_state",
        "cluster_name",
        "mce_name",
        "last_seen_at",
        "stale",
        "reachable",
        "serial",
        "bmc_host",
        "macs",
    }
    assert rows[seen.id]["vendor"] == "cisco"
    assert rows[seen.id]["site_id"] == "tlv"
    assert rows[seen.id]["health"] == "CRITICAL"
    assert rows[seen.id]["maintenance"] == {"enabled": False, "reason": None}
    assert rows[seen.id]["bmc_host"] == "10.0.0.9"
    assert rows[seen.id]["macs"] == ["aa:bb:cc:dd:ee:01"]
    assert rows[seen.id]["stale"] is False
    assert rows[never.id]["stale"] is True
    assert rows[never.id]["last_seen_at"] is None


async def test_rows_etag_is_stable_across_the_cache_and_answers_304(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """The cache miss and the raw-bytes hit carry one ETag; a current client gets 304."""
    client, repo = app_context
    await repo.upsert(_make_server(1))

    first = await client.get("/api/v1/servers/rows")
    second = await client.get("/api/v1/servers/rows")
    assert first.headers["etag"] == second.headers["etag"]
    assert first.content == second.content

    # Past the cache TTL the body is rebuilt from Mongo; unchanged data
    # must give the same bytes, or a poller never sees a 304.
    redis = RedisClientHolder(get_settings())
    await redis.connect()
    await redis.client.flushdb()
    await redis.close()
    rebuilt = await client.get("/api/v1/servers/rows")
    assert rebuilt.headers["etag"] == first.headers["etag"]

    not_modified = await client.get(
        "/api/v1/servers/rows", headers={"If-None-Match": first.headers["etag"]}
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == first.headers["etag"]

    stale_client = await client.get("/api/v1/servers/rows", headers={"If-None-Match": 'W/"nope"'})
    assert stale_client.status_code == 200


async def test_rows_change_after_a_maintenance_write(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    """ADR-0028: an operator write clears the cached rows body too."""
    client, repo = app_context
    server = await repo.upsert(_make_server(1))
    before = await client.get("/api/v1/servers/rows")

    await client.put(f"/api/v1/servers/{server.id}/maintenance", json={"reason": "fan"})

    after = await client.get("/api/v1/servers/rows")
    assert after.headers["etag"] != before.headers["etag"]
    (row,) = after.json()["items"]
    assert row["maintenance"] == {"enabled": True, "reason": "fan"}


async def test_rows_are_gzipped_for_a_gzip_client(
    app_context: tuple[AsyncClient, MongoServerRepository],
) -> None:
    client, repo = app_context
    for i in range(10):
        await repo.upsert(_make_server(i))

    resp = await client.get("/api/v1/servers/rows", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert len(resp.json()["items"]) == 10
