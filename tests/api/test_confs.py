"""API tests for `GET /api/v1/confs` and `/confs/{conf_id}` (docs/adr/0061-confs.md).

Documents are inserted raw, the way the external writer stores them, with an
ObjectId `_id` the API must never return.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.api.test_auth import _auth_enabled_app

from app.domain.services.authz import LoginResult
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.conf_repository import conf_sort_key
from app.infrastructure.mongodb.indexes import CONFS_COLLECTION
from app.main import create_app

_CONF: dict[str, Any] = {
    "conf_id": "CONF200",
    "part_number": "210-AZKL",
    "model": "PowerEdge R650",
    "server_count": 12,
    "cpu": {
        "model": "Intel Xeon Gold 6338",
        "vendor": "Intel",
        "sockets": 2,
        "cores_per_cpu": 32,
        "total_cores_ht": 128,
        "clock_mhz": 2000,
        "max_clock_mhz": 3200,
        "cache_kb": {"l1": 2560, "l2": 40960, "l3": 49152},
    },
    "ram": {"total_gib": 512, "dimms": 16, "per_dimm_gb": 32, "type": ["DDR5"]},
    "disks": [
        {"count": 2, "type": "NVME", "capacity_tb": 0.48},
        {"count": 2, "type": "NVME", "capacity_tb": 1.92},
    ],
    "gpus": [{"count": 2, "model": "NVIDIA H200 SXM"}],
    "gpu_count": 2,
    "gpu_vram_gb": 141,
    "adapters": [{"count": 2, "ports": 2, "speed": 100}],
    "adapter_count": 2,
}


def _conf(conf_id: str, **overrides: Any) -> dict[str, Any]:
    """
    Build one raw conf document.

    Args:
        conf_id (str): Its id.
        **overrides (Any): Top-level fields to replace.

    Returns:
        dict[str, Any]: A document as the writer stores it.
    """
    return {**copy.deepcopy(_CONF), "conf_id": conf_id, **overrides}


@pytest.fixture
async def confs_context() -> AsyncIterator[tuple[AsyncClient, MongoClientHolder]]:
    """
    A running app over an emptied `confs` collection.

    Yields:
        tuple[AsyncClient, MongoClientHolder]: A client bound to the app, and its Mongo.
    """
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        await mongo.db[CONFS_COLLECTION].delete_many({})
        yield client, mongo
        await mongo.db[CONFS_COLLECTION].delete_many({})


async def test_lists_confs_in_natural_order_without_the_mongo_id(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """`CONF9` before `CONF10`, a revision after its base; no `_id` or stored `server_count`."""
    client, mongo = confs_context
    ids = ["CONF10", "CONF-21", "CONF200.2", "CONF9", "CONF200"]
    await mongo.db[CONFS_COLLECTION].insert_many([_conf(i) for i in ids])

    resp = await client.get("/api/v1/confs")

    assert resp.status_code == 200
    body = resp.json()
    assert [item["conf_id"] for item in body["items"]] == [
        "CONF9",
        "CONF10",
        "CONF200",
        "CONF200.2",
        "CONF-21",
    ]
    assert body["unreadable"] == []
    assert "_id" not in body["items"][0]
    assert "server_count" not in body["items"][0]
    assert body["items"][0]["cpu"]["cache_kb"] == {"l1": 2560, "l2": 40960, "l3": 49152}


async def test_a_document_off_the_schema_is_skipped_and_named(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """One bad document must not take the whole list down."""
    client, mongo = confs_context
    await mongo.db[CONFS_COLLECTION].insert_many(
        [_conf("CONF200"), _conf("CONF201", gpu_count="many"), {"model": "orphan"}]
    )

    body = (await client.get("/api/v1/confs")).json()

    assert [item["conf_id"] for item in body["items"]] == ["CONF200"]
    assert body["unreadable"] == ["?", "CONF201"]


async def test_nullable_fields_and_unread_ram_type_round_trip(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """An unmapped conf has no part number; RAM zeros mean the scan failed."""
    client, mongo = confs_context
    ram = {"total_gib": 0, "dimms": 0, "per_dimm_gb": 0, "type": []}
    doc = _conf("CONF-21", part_number=None, ram=ram, gpus=[], gpu_count=0)
    del doc["gpu_vram_gb"]
    await mongo.db[CONFS_COLLECTION].insert_one(doc)

    item = (await client.get("/api/v1/confs/CONF-21")).json()

    assert item["part_number"] is None
    assert item["ram"] == ram
    assert item["gpu_vram_gb"] is None


async def test_reads_numeric_cache_keys_and_ignores_a_stored_name(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """Cache keys may be stored `11`/`12`/`13`; a stored `conf_name` or `name` is not read."""
    client, mongo = confs_context
    doc = _conf("CONF200", conf_name="XE9680 - SXM H200 1T", name="Compute Std R650")
    doc["cpu"]["cache_kb"] = {"11": 2880, "12": 73728, "13": 69120}
    await mongo.db[CONFS_COLLECTION].insert_one(doc)

    item = (await client.get("/api/v1/confs/CONF200")).json()

    assert item["cpu"]["cache_kb"] == {"l1": 2880, "l2": 73728, "l3": 69120}
    assert "conf_name" not in item
    assert "name" not in item


async def test_disks_gpus_and_adapters_round_trip_in_the_writers_shape(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """Disks carry no model or protocol, adapters no model; an older stored one is ignored."""
    client, mongo = confs_context
    doc = _conf("CONF208")
    doc["disks"][0] |= {"model": "Dell DC NVMe PM9A3", "protocol": "PCIe"}
    doc["adapters"][0]["model"] = "Embedded LOM"
    await mongo.db[CONFS_COLLECTION].insert_one(doc)

    item = (await client.get("/api/v1/confs/CONF208")).json()

    assert item["disks"] == _CONF["disks"]
    assert item["gpus"] == _CONF["gpus"]
    assert item["adapters"] == _CONF["adapters"]


async def test_an_adapter_without_ports_or_speed_is_unreadable(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """The earlier `{count, model}` adapter shape is not the writer's any more."""
    client, mongo = confs_context
    old = _conf("CONF201", adapters=[{"count": 1, "model": "Embedded LOM"}])
    await mongo.db[CONFS_COLLECTION].insert_many([_conf("CONF200"), old])

    body = (await client.get("/api/v1/confs")).json()

    assert [item["conf_id"] for item in body["items"]] == ["CONF200"]
    assert body["unreadable"] == ["CONF201"]


async def test_get_one_by_conf_id(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """A dotted id is a plain path segment."""
    client, mongo = confs_context
    await mongo.db[CONFS_COLLECTION].insert_one(_conf("CONF200.2"))

    resp = await client.get("/api/v1/confs/CONF200.2")

    assert resp.status_code == 200
    assert resp.json()["gpu_count"] == 2
    assert resp.json()["gpu_vram_gb"] == 141


async def test_an_unknown_conf_is_404(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """The RFC 9457 body names the id."""
    client, _ = confs_context

    resp = await client.get("/api/v1/confs/CONF404")

    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_an_unreadable_conf_is_500_not_404(
    confs_context: tuple[AsyncClient, MongoClientHolder],
) -> None:
    """A stored but malformed conf exists; saying it does not would mislead."""
    client, mongo = confs_context
    await mongo.db[CONFS_COLLECTION].insert_one(_conf("CONF201", cpu=None))

    resp = await client.get("/api/v1/confs/CONF201")

    assert resp.status_code == 500
    assert "CONF201" in resp.json()["detail"]


async def test_confs_require_a_resolved_caller() -> None:
    """Mounted with every other authenticated router (ADR-0034)."""
    app = _auth_enabled_app(LoginResult.NO_PERMISSION)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        assert (await client.get("/api/v1/confs")).status_code == 401


def test_sort_key_orders_digit_runs_numerically() -> None:
    """The order the UI shows by default."""
    ids = ["CONF200.10", "CONF200.2", "conf3", "CONF-21"]
    assert sorted(ids, key=conf_sort_key) == ["conf3", "CONF200.2", "CONF200.10", "CONF-21"]
