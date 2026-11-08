"""The `KUBEVIRT` collector through the real ingest pipeline into Mongo (ADR-0058).

The correlation proof: a second run over the same VMs creates nothing, and a VM
started or stopped between runs is updated in place, never minted again.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from typing import Any, cast

import pytest
from tests.kubevirt_fixture import KubeVirtFixture, Vm, kubevirt_tree

from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService
from app.domain.enums import HealthSeverity, LinkState, ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.health_policy import HealthPolicy
from app.domain.models.manager import Manager
from app.domain.ports.provider import ProviderServer, ServerIdentity
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.domain.services.health.metrics import build_default_registry
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.kubevirt.provider import KubeVirtProvider

pytestmark = pytest.mark.integration

SITES = site_catalog("")
_SECRET = "test-cursor-secret"
_NS = "kv-ingest"


def _vms(*, kv01_power: str = "On") -> tuple[Vm, ...]:
    return (
        Vm(
            name="ocp4-tlv-kv-01",
            namespace=_NS,
            power=kv01_power,
            serial="KV01-SERIAL-0001",
            cpu_model="Icelake-Server-v2" if kv01_power == "On" else None,
            nics=[("default", "02:5c:00:00:01:01")],
        ),
        Vm(
            name="ocp4-tlv-kv-02",
            namespace=_NS,
            serial=None,
            nics=[("default", "02:5c:00:00:01:02")],
        ),
        Vm(
            name="ocp4-tlv-kv-03",
            namespace=_NS,
            power="Off",
            serial="KV03-SERIAL-0003",
            nics=[("default", "02:5c:00:00:01:03")],
        ),
    )


@pytest.fixture
def facade() -> Iterator[KubeVirtFixture]:
    with KubeVirtFixture(resources=kubevirt_tree(*_vms())) as fixture:
        yield fixture


def _provider(url: str) -> KubeVirtProvider:
    return KubeVirtProvider(
        manager=Manager(
            _id="mgr_kubevirt",
            name="kubevirt",
            type=ManagerType.KUBEVIRT,
            endpoint=url,
            enabled=True,
            audit=AuditFields.new(),
        ),
        url=url,
        username="scan",
        password="secret",
        verify_tls=True,
        ca_bundle="",
        connect_timeout=2.0,
        read_timeout=5.0,
        vm_budget_seconds=10.0,
        run_budget_seconds=30.0,
        concurrency=4,
    )


class _DefaultPolicies:
    """The seeded system defaults, without a policy collection to seed."""

    async def list_all(self) -> list[HealthPolicy]:
        return default_system_policies()


def _service(mongo: MongoClientHolder) -> IngestService:
    return IngestService(
        sites=SITES,
        server_repo=MongoServerRepository(mongo, cursor_secret=_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
        health_service=HealthPolicyService(
            policy_repo=cast(Any, _DefaultPolicies()), registry=build_default_registry()
        ),
    )


async def _servers(mongo: MongoClientHolder) -> dict[str, Any]:
    repo = MongoServerRepository(mongo, cursor_secret=_SECRET)
    page = await repo.list_page(
        filters={"source_provider": "KUBEVIRT"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=50,
        with_count=False,
    )
    return {s.name: s for s in page.items}


@pytest.fixture
async def clean(mongo_holder: MongoClientHolder) -> AsyncGenerator[MongoClientHolder, None]:
    await mongo_holder.db["servers"].delete_many({"source_provider": "KUBEVIRT"})
    yield mongo_holder
    await mongo_holder.db["servers"].delete_many({"source_provider": "KUBEVIRT"})


async def test_a_second_run_creates_nothing(
    clean: MongoClientHolder, facade: KubeVirtFixture
) -> None:
    service = _service(clean)

    first = await service.ingest(_provider(facade.url))
    second = await service.ingest(_provider(facade.url))

    assert (first.created, first.errors) == (3, 0)
    assert (second.created, second.updated, second.unchanged, second.errors) == (0, 0, 3, 0)


async def test_what_is_stored_for_a_vm(clean: MongoClientHolder, facade: KubeVirtFixture) -> None:
    await _service(clean).ingest(_provider(facade.url))
    servers = await _servers(clean)

    kv01 = servers["ocp4-tlv-kv-01"]
    assert kv01.identity.vendor == "kubevirt"
    assert kv01.identity.serial == "KV01-SERIAL-0001"
    assert kv01.site_id == "tlv"
    assert kv01.manager_id == "mgr_kubevirt"
    assert kv01.reachable is True
    assert kv01.hardware.cpu.model == "Icelake-Server-v2"
    assert kv01.hardware.memory.total_bytes == 4 * 1024**3
    assert [c.name for c in kv01.hardware.storage.controllers] == ["VM Disks"]
    assert kv01.hardware.storage.total_bytes == 1024**3
    assert kv01.network.bmc.host == "127.0.0.1"
    assert kv01.profile_template.name is None
    assert kv01.hardware.power.psus == []
    assert kv01.hardware.gpus == []
    # Nothing a VM lacks reads as "could not read", except the RAID verdict no VM has.
    assert kv01.unread_fields == ["hardware.storage.raid"]
    assert kv01.health.network == HealthSeverity.HEALTHY
    assert kv01.health.overall == HealthSeverity.HEALTHY
    assert kv01.health.bmc == HealthSeverity.UNKNOWN
    assert kv01.health.raid == HealthSeverity.UNKNOWN

    assert servers["ocp4-tlv-kv-02"].identity.serial == f"{_NS}.ocp4-tlv-kv-02"

    kv03 = servers["ocp4-tlv-kv-03"]
    assert [i.link_state for i in kv03.network.interfaces] == [LinkState.UNKNOWN]
    assert kv03.hardware.cpu.model is None
    assert "hardware.cpu.model" not in kv03.unread_fields
    assert kv03.health.overall == HealthSeverity.UNKNOWN


async def test_stopping_a_vm_updates_it_in_place(
    clean: MongoClientHolder, facade: KubeVirtFixture
) -> None:
    service = _service(clean)
    await service.ingest(_provider(facade.url))
    before = (await _servers(clean))["ocp4-tlv-kv-01"]

    facade.resources = kubevirt_tree(*_vms(kv01_power="Off"))
    summary = await service.ingest(_provider(facade.url))
    after = (await _servers(clean))["ocp4-tlv-kv-01"]

    assert (summary.created, summary.updated, summary.unchanged) == (0, 1, 2)
    assert after.id == before.id
    assert after.hardware.cpu.model is None
    assert "hardware.cpu.model" not in after.unread_fields
    assert [i.link_state for i in after.network.interfaces] == [LinkState.UNKNOWN]
    assert after.health.overall == HealthSeverity.UNKNOWN


async def test_a_failed_drive_read_keeps_the_stored_disks(
    clean: MongoClientHolder, facade: KubeVirtFixture
) -> None:
    service = _service(clean)
    await service.ingest(_provider(facade.url))
    vm = _vms()[0]

    facade.faults[f"{vm.path}/Storage/1/Drives/scratch"] = 500
    await service.ingest(_provider(facade.url))
    kv01 = (await _servers(clean))["ocp4-tlv-kv-01"]

    assert len(kv01.hardware.storage.drives) == 2
    assert kv01.hardware.storage.total_bytes == 1024**3
    assert "hardware.storage.drives" in kv01.unread_fields


async def test_a_deleted_vm_is_not_an_error_and_the_rest_are_unchanged(
    clean: MongoClientHolder, facade: KubeVirtFixture
) -> None:
    service = _service(clean)
    await service.ingest(_provider(facade.url))

    facade.resources = kubevirt_tree(*_vms()[:2])
    provider = _provider(facade.url)
    summary = await service.ingest(provider)

    assert (summary.fetched, summary.unchanged, summary.errors) == (2, 2, 0)
    assert provider.collection_errors == ()


async def test_a_recheck_writes_the_same_document(
    clean: MongoClientHolder, facade: KubeVirtFixture
) -> None:
    service = _service(clean)
    await service.ingest(_provider(facade.url))
    stored = (await _servers(clean))["ocp4-tlv-kv-01"]

    fresh = await _provider(facade.url).get_one(
        ServerIdentity(external_id=stored.identity.external_ids["mgr_kubevirt"])
    )
    assert isinstance(fresh, ProviderServer)
    updated = await service.ingest_one(fresh, provider_type="KUBEVIRT")

    assert updated.id == stored.id
    assert updated.revision == stored.revision
