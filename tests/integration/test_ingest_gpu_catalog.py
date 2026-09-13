"""
`IngestService`-level proof that `gpu_catalog` enrichment reaches a stored
`Server` — survives carry-forward, pydantic construction and the Mongo
round-trip — not just `GpuCatalog.enrich()` (`tests/unit/domain/test_gpu_catalog.py`).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from app.application.services.ingest import IngestService
from app.domain.models.hardware import Gpu
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.gpu_catalog import GpuCatalog
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

SITES = site_catalog("")
_CATALOG = GpuCatalog.from_spec("P1001-200:NVIDIA A100 40GB:40")

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


class _OneShotProvider(ServerInventoryProvider):
    """Yields exactly the `ProviderServer`s it is handed."""

    provider_type = "test"

    def __init__(self, *servers: ProviderServer) -> None:
        super().__init__()
        self._servers = servers

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        raise NotImplementedError

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        for server in self._servers:
            yield server


def _service(mongo: MongoClientHolder, *, gpu_catalog: GpuCatalog = _CATALOG) -> IngestService:
    return IngestService(
        sites=SITES,
        gpu_catalog=gpu_catalog,
        server_repo=MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
    )


def _with_one_gpu(**overrides: Any) -> ProviderServer:
    """A server whose collector reported one GPU by PID only, the shape
    UCS Manager's `graphicsCard` and Intersight's `graphics.Card` both
    produce — no memory size, since neither API reports one.
    """
    base: dict[str, Any] = {
        "external_id": "ucsm://domain-1/sys/chassis-1/blade-1",
        "vendor": "cisco",
        "name": "ocp4-prod-tlv-infra-02",
        "serial": "SN-GPU-1",
        "gpus": ({"vendor": "NVIDIA", "model": "P1001-200", "memory_bytes": None},),
    }
    base.update(overrides)
    return ProviderServer(**base)


async def _stored_gpus(mongo: MongoClientHolder, serial_normalized: str) -> list[Gpu]:
    repo = MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": serial_normalized},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    return page.items[0].hardware.gpus


async def test_a_known_pid_is_enriched_all_the_way_to_the_stored_server(
    mongo_holder: MongoClientHolder,
) -> None:
    """A PID this deployment's `INVENTORY_GPU_MODELS` recognizes comes
    back out of Mongo with a friendly name and a real VRAM size, not the
    bare PID and `memory_bytes: None` the collector actually reported.
    """
    service = _service(mongo_holder)

    await service.ingest(_OneShotProvider(_with_one_gpu()))

    gpus = await _stored_gpus(mongo_holder, "sn-gpu-1")
    assert [g.model for g in gpus] == ["NVIDIA A100 40GB"]
    assert [g.memory_bytes for g in gpus] == [40 * 1024**3]


async def test_an_identifier_nothing_recognizes_is_stored_unchanged(
    mongo_holder: MongoClientHolder,
) -> None:
    """`P1001-200` is in neither the built-in table nor this spec, and an
    unrecognized identifier must not fail or invent a value — it stores
    exactly what the collector reported.
    """
    service = _service(mongo_holder, gpu_catalog=GpuCatalog.from_spec(""))

    await service.ingest(_OneShotProvider(_with_one_gpu(serial="SN-GPU-2")))

    gpus = await _stored_gpus(mongo_holder, "sn-gpu-2")
    assert [g.model for g in gpus] == ["P1001-200"]
    assert [g.memory_bytes for g in gpus] == [None]


async def test_a_real_reported_memory_value_survives_the_full_pipeline_unchanged(
    mongo_holder: MongoClientHolder,
) -> None:
    """A provider's value always wins over a filled-in default — proven for
    carry-forward in `test_ingest_partial_reads.py`, here for catalog enrichment.
    """
    service = _service(mongo_holder)

    await service.ingest(
        _OneShotProvider(
            _with_one_gpu(
                serial="SN-GPU-3",
                gpus=({"vendor": "NVIDIA", "model": "P1001-200", "memory_bytes": 12345},),
            )
        )
    )

    gpus = await _stored_gpus(mongo_holder, "sn-gpu-3")
    assert [g.model for g in gpus] == ["P1001-200"]
    assert [g.memory_bytes for g in gpus] == [12345]


async def test_a_vendor_model_string_is_enriched_from_the_built_in_table(
    mongo_holder: MongoClientHolder,
) -> None:
    """ADR-0021's multi-vendor half: a Dell or HPE BMC reports only a model
    string, and an unconfigured deployment must still resolve it end to end.
    """
    service = _service(mongo_holder, gpu_catalog=GpuCatalog.from_spec(""))

    await service.ingest(
        _OneShotProvider(
            _with_one_gpu(
                serial="SN-GPU-4",
                gpus=({"vendor": "NVIDIA", "model": "NVIDIA A100-PCIE-40GB"},),
            )
        )
    )

    gpus = await _stored_gpus(mongo_holder, "sn-gpu-4")
    assert [g.model for g in gpus] == ["NVIDIA A100 40GB"]
    assert [g.memory_bytes for g in gpus] == [40 * 1024**3]
