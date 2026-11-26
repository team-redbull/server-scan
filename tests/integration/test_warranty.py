"""Warranty: a new server and "Fetch now" look it up; a monthly sync; physical only (ADR-0070)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pymongo.errors import ServerSelectionTimeoutError
from tools.sync_warranty import sync_warranty

from app.application.services.ingest import IngestService
from app.config.settings import get_settings
from app.domain.enums import UnreachableReason
from app.domain.models.warranty import Warranty
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.services.normalize import normalize_text
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder, warranty_source
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.mongodb.warranty_source import MongoWarrantySource

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


class _Source:
    """A `WarrantySource` over a dict, counting lookups."""

    def __init__(self, records: dict[str, Warranty] | None) -> None:
        self.records = records
        self.lookups: list[str] = []

    async def all(self) -> dict[str, Warranty] | None:
        return self.records

    async def one(self, serial: str) -> Warranty | None:
        self.lookups.append(serial)
        if self.records is None:
            return None
        return self.records.get(normalize_text(serial)) or Warranty(found=False)


class _Provider(ServerInventoryProvider):
    """Yields the records it is handed."""

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


def _record(**overrides: Any) -> ProviderServer:
    """A minimal physical server."""
    base: dict[str, Any] = {
        "external_id": "x-1",
        "vendor": "cisco",
        "name": "ocp-tomer-compute-o01",
        "serial": "WMP25492815",
        "nic_macs": ("00:00:5e:00:53:01",),
    }
    base.update(overrides)
    return ProviderServer(**base)


def _service(mongo: MongoClientHolder, source: _Source) -> IngestService:
    return IngestService(
        sites=site_catalog(""),
        server_repo=MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
        warranty=source,
    )


async def _only(mongo: MongoClientHolder) -> Any:
    repo = MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=10,
        with_count=False,
    )
    return page.items


_ACTIVE = {"wmp25492815": Warranty(found=True, expires_on="2027-01-29")}


async def test_a_new_server_is_looked_up_and_an_existing_one_is_not(
    mongo_holder: MongoClientHolder,
) -> None:
    """A daily run reads the warranty collection only for a server it creates."""
    source = _Source(_ACTIVE)
    service = _service(mongo_holder, source)
    await service.ingest(_Provider(_record()))
    await service.ingest(_Provider(_record()))
    [server] = await _only(mongo_holder)
    assert server.warranty == Warranty(found=True, expires_on="2027-01-29")
    assert source.lookups == ["WMP25492815"]


async def test_an_existing_server_keeps_its_warranty_across_runs(
    mongo_holder: MongoClientHolder,
) -> None:
    """A run that does not look it up must not erase what the last lookup stored."""
    await _service(mongo_holder, _Source(_ACTIVE)).ingest(_Provider(_record()))
    later = _Source({})
    await _service(mongo_holder, later).ingest(_Provider(_record(name="renamed")))
    [server] = await _only(mongo_holder)
    assert server.warranty is not None and server.warranty.expires_on == "2027-01-29"
    assert later.lookups == []


async def test_fetch_now_looks_the_warranty_up_again(mongo_holder: MongoClientHolder) -> None:
    """`ingest_one(refresh_warranty=True)` is what "Fetch now" calls."""
    await _service(mongo_holder, _Source({})).ingest(_Provider(_record()))
    renewed = _Source({"wmp25492815": Warranty(found=True, expires_on="2030-01-01")})
    server = await _service(mongo_holder, renewed).ingest_one(
        _record(), provider_type="test", refresh_warranty=True
    )
    assert server.warranty == Warranty(found=True, expires_on="2030-01-01")


async def test_an_unreadable_collection_keeps_the_stored_warranty(
    mongo_holder: MongoClientHolder,
) -> None:
    """A failed read is never "no record"."""
    await _service(mongo_holder, _Source(_ACTIVE)).ingest(_Provider(_record()))
    server = await _service(mongo_holder, _Source(None)).ingest_one(
        _record(), provider_type="test", refresh_warranty=True
    )
    assert server.warranty is not None and server.warranty.found


async def test_a_vm_is_never_looked_up(mongo_holder: MongoClientHolder) -> None:
    """A KubeVirt VM has no warranty."""
    source = _Source(_ACTIVE)
    await _service(mongo_holder, source).ingest(
        _Provider(_record(vendor="kubevirt", serial="WMP25492815"))
    )
    [server] = await _only(mongo_holder)
    assert server.warranty is None
    assert source.lookups == []


async def test_the_monthly_sync_writes_physical_servers_only_and_is_idempotent(
    mongo_holder: MongoClientHolder,
) -> None:
    """It bumps the revision only for a real change, and never touches a VM."""
    await _service(mongo_holder, _Source({})).ingest(
        _Provider(
            _record(),
            _record(external_id="x-2", serial="NOREC1", name="b-server"),
            _record(external_id="vm-1", vendor="kubevirt", serial="VM1", name="c-vm"),
        )
    )
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    before = {s.name: s.revision for s in await _only(mongo_holder)}

    first = await sync_warranty(repo, _ACTIVE)
    second = await sync_warranty(repo, _ACTIVE)

    after = {s.name: s for s in await _only(mongo_holder)}
    assert after["ocp-tomer-compute-o01"].warranty == _ACTIVE["wmp25492815"]
    assert after["ocp-tomer-compute-o01"].revision == before["ocp-tomer-compute-o01"] + 1
    assert after["b-server"].warranty == Warranty(found=False)
    assert after["c-vm"].warranty is None
    assert after["c-vm"].revision == before["c-vm"]
    assert (first.updated, second.updated) == (1, 0)


async def test_the_sync_does_not_overwrite_a_fetch_now_that_won_the_race(
    mongo_holder: MongoClientHolder,
) -> None:
    """After a CAS miss it re-reads; a server already holding the wanted value is left alone."""
    await _service(mongo_holder, _Source({})).ingest(_Provider(_record()))

    class _Racing(MongoServerRepository):
        """Lets a concurrent writer set the same warranty first, so the first CAS misses."""

        raced = False

        async def set_warranty(
            self, server_id: str, warranty: dict[str, Any] | None, *, expected_revision: int
        ) -> bool:
            if not self.raced:
                self.raced = True
                await super().set_warranty(server_id, warranty, expected_revision=expected_revision)
                return False
            return await super().set_warranty(
                server_id, warranty, expected_revision=expected_revision
            )

    repo = _Racing(mongo_holder, cursor_secret=_CURSOR_SECRET)
    [before] = await _only(mongo_holder)
    summary = await sync_warranty(repo, _ACTIVE)
    [after] = await _only(mongo_holder)
    assert (summary.updated, summary.unchanged, summary.conflicts) == (0, 1, 0)
    assert after.revision == before.revision + 1


async def test_an_adopted_serial_less_stub_is_looked_up(mongo_holder: MongoClientHolder) -> None:
    """A standalone stub that later reads its serial is identified now, so it gets its warranty."""
    source = _Source(_ACTIVE)
    service = _service(mongo_holder, source)

    class _Standalone(_Provider):
        provider_type = "REDFISH_STANDALONE"

    stub = _record(
        vendor="standalone",
        serial=None,
        nic_macs=None,
        bmc_address_raw="10.0.0.9",
        reachable=False,
        unreachable_reason=UnreachableReason.NETWORK_UNREACHABLE,
    )
    await service.ingest(_Standalone(stub))
    await service.ingest(_Standalone(_record(vendor="standalone", bmc_address_raw="10.0.0.9")))
    servers = await _only(mongo_holder)
    assert [s.warranty for s in servers] == [_ACTIVE["wmp25492815"]]
    assert source.lookups == ["WMP25492815"]


async def test_a_warranty_never_changes_health(mongo_holder: MongoClientHolder) -> None:
    """An expired warranty is informational only (ADR-0070)."""
    await _service(mongo_holder, _Source(None)).ingest(_Provider(_record()))
    [without] = await _only(mongo_holder)
    expired = _Source({"wmp25492815": Warranty(found=True, expires_on="2001-01-01")})
    server = await _service(mongo_holder, expired).ingest_one(
        _record(), provider_type="test", refresh_warranty=True
    )
    assert server.warranty is not None and server.warranty.expires_on == "2001-01-01"
    assert server.health == without.health


async def test_the_real_lookup_matches_case_spaces_and_numeric_serials(
    mongo_holder: MongoClientHolder,
) -> None:
    """`one()` against Mongo agrees with `all()`: case, inner spaces and a number stored as one."""
    collection = mongo_holder.db["warranty_test"]
    await collection.delete_many({})
    await collection.insert_many(
        [
            {"Serial": " ab  12 ", "Warranty": "29-01-2027"},
            {"Serial": 12345, "Warranty": "01-02-2027"},
            {"Serial": 678.0, "Warranty": "02-02-2027"},
        ]
    )
    try:
        source = MongoWarrantySource(
            get_settings().mongo_uri, mongo_holder.db.name, "warranty_test"
        )
        assert await source.one("AB 12") == Warranty(found=True, expires_on="2027-01-29")
        assert await source.one("12345") == Warranty(found=True, expires_on="2027-02-01")
        assert await source.one("678") == Warranty(found=True, expires_on="2027-02-02")
        assert await source.one("missing") == Warranty(found=False)
        assert await source.one("To Be Filled By O.E.M.") is None
        assert set(await source.all() or {}) == {"ab 12", "12345", "678"}
    finally:
        await collection.drop()


async def test_an_unreachable_warranty_database_is_tried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A down database costs one timeout per source, then every lookup is "not read"."""
    calls: list[str] = []

    def _down(uri: str) -> Any:
        calls.append(uri)
        raise ServerSelectionTimeoutError("down")

    monkeypatch.setattr(warranty_source, "_client", _down)
    source = MongoWarrantySource("mongodb://warranty", "db", "coll")
    assert await source.one("A1") is None
    assert await source.one("A2") is None
    assert await source.all() is None
    assert calls == ["mongodb://warranty"]


async def test_the_sync_never_restores_an_older_date_over_a_newer_fetch_now(
    mongo_holder: MongoClientHolder,
) -> None:
    """A writer that changed the warranty mid-sync wins: the sync's snapshot is older."""
    await _service(mongo_holder, _Source({})).ingest(_Provider(_record()))

    class _Racing(MongoServerRepository):
        """A "Fetch now" stores `found=False` just before the sync's first CAS."""

        raced = False

        async def set_warranty(
            self, server_id: str, warranty: dict[str, Any] | None, *, expected_revision: int
        ) -> bool:
            if not self.raced:
                self.raced = True
                await super().set_warranty(
                    server_id,
                    {"found": False, "expires_on": None},
                    expected_revision=expected_revision,
                )
                return False
            return await super().set_warranty(
                server_id, warranty, expected_revision=expected_revision
            )

    await MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET).set_warranty(
        (await _only(mongo_holder))[0].id,
        None,
        expected_revision=(await _only(mongo_holder))[0].revision,
    )
    repo = _Racing(mongo_holder, cursor_secret=_CURSOR_SECRET)
    summary = await sync_warranty(repo, _ACTIVE)
    [after] = await _only(mongo_holder)
    assert after.warranty == Warranty(found=False)
    assert summary.updated == 0
