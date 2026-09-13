"""
Regression tests for the defects ADR-0016 found in shipped code. One root
cause: the pipeline could not tell "read this and found nothing" from
"could not read this" — DSP0266 §9.6.1's absent-versus-null distinction.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from app.application.services.ingest import IngestService
from app.domain.enums import HealthSeverity, MediaType
from app.domain.models.health import Health
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

SITES = site_catalog("")

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


class _OneShotProvider(ServerInventoryProvider):
    """Yields exactly the `ProviderServer`s it is handed.

    Lets a test state a provider's output directly, including the `None`s a
    real collector emits for a sub-resource it could not read.
    """

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


def _service(mongo: MongoClientHolder) -> IngestService:
    return IngestService(
        sites=SITES,
        server_repo=MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
    )


def _fully_read(**overrides: Any) -> ProviderServer:
    """A server whose collector read every field successfully."""
    base: dict[str, Any] = {
        "external_id": "redfish://10.20.30.41/redfish/v1/Systems/1",
        "vendor": "dell",
        "name": "ocp4-prod-tlv-infra-01",
        "serial": "SN-PARTIAL-1",
        "system_uuid": "11111111-2222-3333-4444-555555555555",
        "nic_macs": ("00:00:5e:00:53:01",),
        "cpu_sockets": 2,
        "cpu_cores": 64,
        "cpu_threads": 128,
        "cpu_model": "Xeon Gold 6338",
        "memory_total_bytes": 512 * 1024**3,
        "storage_total_bytes": 4 * 1024**4,
        "storage_drives": (
            {
                "id": "/redfish/v1/Chassis/1/Drives/0",
                "model": "MZ7LH3T8",
                "serial": "DRIVE-1",
                "media_type": MediaType.SSD.value,
                "capacity_bytes": 4 * 1024**4,
                "health": HealthSeverity.CRITICAL.value,
                "health_detail": "self-test-failed",
            },
        ),
        "psus": (
            {
                "id": "1",
                "model": "PSU-750W",
                "serial": "PSU-1",
                "health": "DOWN",
                "health_detail": "inoperable",
            },
        ),
        # Redfish is the only provider that reports DIMM health, and this
        # fixture is a Dell reached over Redfish — so "fully read" has to
        # include it, or every assertion here flags it as unread.
        "memory_modules": (
            {
                "slot": "DIMM.Socket.A1",
                "size_bytes": 64 * 1024**3,
                "type": "DDR5",
                "speed_mhz": 4800,
                "serial": "DIMM-1",
                "health": HealthSeverity.HEALTHY.value,
            },
        ),
    }
    base.update(overrides)
    return ProviderServer(**base)


async def test_a_sub_resource_that_could_not_be_read_does_not_erase_stored_hardware(
    mongo_holder: MongoClientHolder,
) -> None:
    """The defect behind ADR-0016's port change: a 404ing `Storage` collection
    used to write zeros over good data, so a server with a failed disk healed
    itself by not being read.
    """
    service = _service(mongo_holder)

    await service.ingest(_OneShotProvider(_fully_read()))

    # Next run every sub-resource failed: `None` means "don't know", not zero.
    summary = await service.ingest(
        _OneShotProvider(
            _fully_read(
                cpu_sockets=None,
                cpu_cores=None,
                cpu_threads=None,
                cpu_model=None,
                memory_total_bytes=None,
                storage_total_bytes=None,
                storage_drives=None,
                nic_macs=None,
                psus=None,
            )
        )
    )

    # Without the carry-forward this is 1: pydantic rejects `Cpu(sockets=None)`
    # and the stored document survives only by accident, so the asserts
    # below are not sufficient on their own.
    assert summary.errors == 0
    assert summary.updated == 1

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-1"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    server = page.items[0]

    assert server.hardware.storage.total_bytes == 4 * 1024**4
    assert [d.health for d in server.hardware.storage.drives] == [HealthSeverity.CRITICAL.value]
    assert server.hardware.cpu.sockets == 2
    assert server.hardware.cpu.model == "Xeon Gold 6338"
    assert server.hardware.memory.total_bytes == 512 * 1024**3
    assert server.identity.nic_macs == ["00:00:5e:00:53:01"]
    # `psus` follows the same carry-forward contract — `IngestService` used to
    # hardcode `Power(psus=[])` unconditionally.
    assert [p.health for p in server.hardware.power.psus] == ["DOWN"]
    # `health_detail` (the raw vendor state) crosses `_drive_from_dict`/
    # `_psu_from_dict` and survives the same carry-forward as `health`.
    assert [d.health_detail for d in server.hardware.storage.drives] == ["self-test-failed"]
    assert [p.health_detail for p in server.hardware.power.psus] == ["inoperable"]


async def test_a_profile_template_that_could_not_be_read_survives(
    mongo_holder: MongoClientHolder,
) -> None:
    """`profile_template_name`/`_external_id` used to be written with no
    carry-forward, so a transient failure of a vendor's template lookup
    silently blanked an already-known template (fixed 2026-09-08).
    """
    service = _service(mongo_holder)

    await service.ingest(
        _OneShotProvider(
            _fully_read(
                profile_template_name="worker-profile-tmpl",
                profile_template_external_id="tmpl-001",
            )
        )
    )

    summary = await service.ingest(
        _OneShotProvider(_fully_read(profile_template_name=None, profile_template_external_id=None))
    )
    assert summary.errors == 0
    assert summary.updated == 1

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-1"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    server = page.items[0]

    assert server.profile_template.name == "worker-profile-tmpl"
    assert server.profile_template.external_id == "tmpl-001"
    assert "profile_template.name" in server.unread_fields
    assert "profile_template.external_id" in server.unread_fields


async def test_gpu_health_detail_survives_the_full_ingest_pipeline(
    mongo_holder: MongoClientHolder,
) -> None:
    """The same `health_detail` round trip as the drive/PSU one above,
    for `IngestService._gpu_from_dict` — not covered by `_fully_read`'s
    own base fixture, which reports no GPU by default.
    """
    service = _service(mongo_holder)

    await service.ingest(
        _OneShotProvider(
            _fully_read(
                serial="SN-GPU-DETAIL-1",
                gpus=(
                    {
                        "vendor": "NVIDIA",
                        "model": "H100",
                        "serial": "GPU-1",
                        "health": HealthSeverity.CRITICAL.value,
                        "health_detail": "hardware-failure",
                    },
                ),
            )
        )
    )

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-gpu-detail-1"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    [gpu] = page.items[0].hardware.gpus
    assert gpu.health_detail == "hardware-failure"


async def test_an_empty_read_still_overwrites(mongo_holder: MongoClientHolder) -> None:
    """The other half of the contract: a collector that read a host and found
    no drives must be able to say so — which is why `None` is distinct from zero.
    """
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_fully_read(serial="SN-PARTIAL-2")))
    await service.ingest(
        _OneShotProvider(
            _fully_read(serial="SN-PARTIAL-2", storage_total_bytes=0, storage_drives=())
        )
    )

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-2"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    assert page.items[0].hardware.storage.drives == []
    assert page.items[0].hardware.storage.total_bytes == 0


async def test_a_partial_read_does_not_write_a_health_recovery_event(
    mongo_holder: MongoClientHolder,
) -> None:
    """The defect's second-order harm: zeroed storage also wrote a durable
    HEALTH_STATUS_CHANGED event asserting the failed drive had recovered.
    """
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_fully_read(serial="SN-PARTIAL-3")))

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-3"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    stored = page.items[0]
    stored.health = Health(overall=HealthSeverity.CRITICAL, storage=HealthSeverity.CRITICAL)
    await repo.upsert(stored)

    summary = await service.ingest(
        _OneShotProvider(
            _fully_read(serial="SN-PARTIAL-3", storage_total_bytes=None, storage_drives=None)
        )
    )
    assert summary.errors == 0

    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-3"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    assert page.items[0].health.overall is HealthSeverity.CRITICAL


async def test_two_servers_without_a_system_uuid_can_both_be_ingested(
    mongo_holder: MongoClientHolder,
) -> None:
    """`$exists: true` matches a present-and-null field, so every UUID-less
    server used to enter a unique index keyed on null and only one could
    exist fleet-wide (ADR-0016, `docs/notes/redfish-plan.md` 1.1).
    """
    # `mongo_holder` already ran `ensure_indexes`; re-creating the index here
    # would race the fixture's own migration.
    service = _service(mongo_holder)
    summary = await service.ingest(
        _OneShotProvider(
            _fully_read(serial="SN-NOUUID-1", system_uuid=None, name="ocp4-one-a"),
            _fully_read(serial="SN-NOUUID-2", system_uuid=None, name="ocp4-one-b"),
        )
    )

    assert summary.errors == 0
    assert summary.fetched == 2


async def test_two_different_servers_sharing_a_system_uuid_both_ingest(
    mongo_holder: MongoClientHolder,
) -> None:
    """A live UCS domain reported one `system_uuid` for two servers (a UUID
    Suffix Pool misconfiguration); the unique index failed the second forever.
    Dropped 2026-09-09 — correlation is `vendor`+`serial_normalized` (`indexes.py`).
    """
    service = _service(mongo_holder)
    summary = await service.ingest(
        _OneShotProvider(
            _fully_read(
                serial="SN-DUPE-UUID-1",
                name="ocp4-prod-tlv-infra-01",
                system_uuid="11111111-2222-3333-4444-555555555555",
            ),
            _fully_read(
                serial="SN-DUPE-UUID-2",
                name="ocp4-prod-nyc-infra-01",
                system_uuid="11111111-2222-3333-4444-555555555555",
            ),
        )
    )

    assert summary.errors == 0
    assert summary.fetched == 2

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for serial in ("sn-dupe-uuid-1", "sn-dupe-uuid-2"):
        page = await repo.list_page(
            filters={"identity.serial_normalized": serial},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=1,
            with_count=False,
        )
        assert len(page.items) == 1
        assert page.items[0].identity.system_uuid == "11111111-2222-3333-4444-555555555555"


async def test_the_document_records_which_fields_this_run_could_not_read(
    mongo_holder: MongoClientHolder,
) -> None:
    """`unread_fields` names what this run could not read and is recomputed,
    never merged, so the re-ingest half matters most: a merged list would keep
    every field flagged forever (CLAUDE.md, `Server.unread_fields`).
    """
    service = _service(mongo_holder)
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)

    async def stored() -> list[str]:
        page = await repo.list_page(
            filters={"identity.serial_normalized": "sn-unread-1"},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=1,
            with_count=False,
        )
        return page.items[0].unread_fields

    # An iLO-4 shape: identity intact, every subresource call refused.
    await service.ingest(
        _OneShotProvider(
            _fully_read(
                serial="SN-UNREAD-1",
                nic_macs=None,
                storage_total_bytes=None,
                storage_drives=None,
                gpus=None,
            )
        )
    )

    assert sorted(await stored()) == [
        "hardware.gpus",
        "hardware.storage.drives",
        "hardware.storage.total_bytes",
        "identity.nic_macs",
        # `_fully_read()`'s base never sets these, so they read as unread.
        "profile_template.external_id",
        "profile_template.name",
    ]

    # `gpus=()` is a real answer ("none installed"), so it clears the flag too.
    await service.ingest(
        _OneShotProvider(
            _fully_read(
                serial="SN-UNREAD-1",
                gpus=(),
                profile_template_name="worker-profile-tmpl",
                profile_template_external_id="tmpl-001",
            )
        )
    )

    assert await stored() == []
