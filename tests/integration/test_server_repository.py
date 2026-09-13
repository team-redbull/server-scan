"""
Integration tests for `MongoServerRepository` against the live dev Mongo:
round-trip, unique indexes, keyset pagination, filters, search, sort, and
that the search query is an IXSCAN.
"""

from __future__ import annotations

import json

import pytest
from pymongo.errors import DuplicateKeyError

from app.domain.enums import HealthSeverity, InstallationType, OpenShiftState, Vendor
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.domain.services.search import build_search_query
from app.domain.services.search_tokens import build_search_tokens
from app.errors import (
    CursorFilterMismatchError,
    CursorInvalidError,
    NotFoundError,
    RevisionConflictError,
    UnknownSortFieldError,
)
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


def _make_server(
    index: int,
    *,
    site_id: str | None = None,
    vendor: Vendor = Vendor.DELL,
    health: HealthSeverity = HealthSeverity.UNKNOWN,
    installation_type: InstallationType = InstallationType.UNCLASSIFIED,
    maintenance_enabled: bool = False,
    name: str | None = None,
    serial: str | None = None,
    system_uuid: str | None = None,
) -> Server:
    now = utcnow()
    nm = name if name is not None else f"srv-{index:04d}"
    ser = serial if serial is not None else f"SN{index:06d}"
    uid = system_uuid if system_uuid is not None else f"uuid-{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=nm,
        name_normalized=normalize_text(nm),
        identity=Identity(
            vendor=vendor, serial=ser, serial_normalized=normalize_text(ser), system_uuid=uid
        ),
        site_id=site_id,
        classification=Classification(installation_type=installation_type),
        health=Health(overall=health),
        maintenance=Maintenance(enabled=maintenance_enabled),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


async def test_insert_and_get_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    server = _make_server(1)

    await repo.upsert(server)
    fetched = await repo.get_by_id(server.id)

    assert fetched is not None
    assert fetched.id == server.id
    assert fetched.name == server.name
    assert fetched.identity.serial == server.identity.serial


async def test_get_by_id_missing_returns_none(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    assert await repo.get_by_id("srv_does_not_exist") is None


async def test_duplicate_system_uuid_no_longer_raises(mongo_holder: MongoClientHolder) -> None:
    """Reversed 2026-09-09: a UCS `uuid` is the service profile's, from an
    admin-managed pool, so two real servers can share one. Still indexed, no
    longer unique — see `app.infrastructure.mongodb.indexes`'s module docstring.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    first = _make_server(1, system_uuid="uuid-shared")
    second = _make_server(2, system_uuid="uuid-shared")

    await repo.upsert(first)
    await repo.upsert(second)

    stored_first = await repo.get_by_id(first.id)
    stored_second = await repo.get_by_id(second.id)
    assert stored_first is not None
    assert stored_second is not None
    assert stored_first.identity.system_uuid == "uuid-shared"
    assert stored_second.identity.system_uuid == "uuid-shared"


async def test_duplicate_vendor_serial_raises_duplicate_key_error(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    first = _make_server(1, vendor=Vendor.DELL, serial="SHARED123", system_uuid="uuid-a")
    second = _make_server(2, vendor=Vendor.DELL, serial="SHARED123", system_uuid="uuid-b")

    await repo.upsert(first)
    with pytest.raises(DuplicateKeyError):
        await repo.upsert(second)


async def test_pagination_covers_every_document_exactly_once(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    total = 53
    for i in range(total):
        await repo.upsert(_make_server(i))

    seen: list[str] = []
    cursor: str | None = None
    page_size = 10
    for _ in range(total):  # generous upper bound on iterations
        page = await repo.list_page(
            filters={},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=cursor,
            page_size=page_size,
            with_count=False,
        )
        seen.extend(item.id for item in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor
        assert cursor is not None

    assert len(seen) == total
    assert len(set(seen)) == total


async def test_filters_narrow_results(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(5):
        await repo.upsert(_make_server(i, site_id="tlv"))
    for i in range(5, 8):
        await repo.upsert(_make_server(i, site_id="nyc"))

    page = await repo.list_page(
        filters={"site_id": "tlv"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=50,
        with_count=True,
    )

    assert len(page.items) == 5
    assert all(item.site_id == "tlv" for item in page.items)
    assert page.total_count == 5


async def test_search_matches_by_token(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    await repo.upsert(_make_server(1, name="ocp-dell-worker-001"))
    await repo.upsert(_make_server(2, name="upi-cisco-master-002"))
    await repo.upsert(_make_server(3, name="random-server-0003"))

    page = await repo.list_page(
        filters={},
        search="ocp",
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=50,
        with_count=False,
    )

    assert len(page.items) == 1
    assert page.items[0].name == "ocp-dell-worker-001"


async def test_sort_ascending_and_descending(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    names = ["bravo", "alpha", "charlie"]
    for i, name in enumerate(names):
        await repo.upsert(_make_server(i, name=name))

    ascending = await repo.list_page(
        filters={},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=50,
        with_count=False,
    )
    descending = await repo.list_page(
        filters={},
        search=None,
        sort="name",
        sort_desc=True,
        cursor=None,
        page_size=50,
        with_count=False,
    )

    assert [s.name for s in ascending.items] == ["alpha", "bravo", "charlie"]
    assert [s.name for s in descending.items] == ["charlie", "bravo", "alpha"]


async def test_sorting_by_openshift_state_pages_without_gaps(
    mongo_holder: MongoClientHolder,
) -> None:
    """A new sort field is only safe once a keyset walk over it is proven.

    A `SORT_FIELDS`/`SORT_ACCESSORS`/index mismatch shows up as a skipped
    or repeated server, never as an error.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    states = [
        OpenShiftState.INSTALLED,
        OpenShiftState.AVAILABLE,
        OpenShiftState.INSTALLED_TO_INVENTORY,
    ]
    for index in range(12):
        server = _make_server(index)
        server.openshift = OpenShiftLifecycle(lifecycle_state=states[index % 3])
        await repo.upsert(server)

    for descending in (False, True):
        seen: list[str] = []
        order: list[str] = []
        cursor: str | None = None
        while True:
            page = await repo.list_page(
                filters={},
                search=None,
                sort="openshift_state",
                sort_desc=descending,
                cursor=cursor,
                page_size=5,
                with_count=False,
            )
            seen.extend(item.id for item in page.items)
            order.extend(item.openshift.lifecycle_state.value for item in page.items)
            if not page.has_more or page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert len(seen) == len(set(seen)) == 12
        assert order == sorted(order, reverse=descending)


async def test_sorting_by_a_nullable_field_pages_without_gaps(
    mongo_holder: MongoClientHolder,
) -> None:
    """Mongo's range operators are type-bracketed, which silently drops rows.

    A naive cursor strands every server no cluster holds (ADR-0026).
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    clusters = ["ocp4-tlv", None, "ocp4-nyc", None, "hc-tlv-01"]
    for index in range(15):
        server = _make_server(index)
        server.openshift = OpenShiftLifecycle(cluster_name=clusters[index % 5])
        await repo.upsert(server)

    for descending in (False, True):
        seen: list[str] = []
        order: list[str | None] = []
        cursor: str | None = None
        while True:
            page = await repo.list_page(
                filters={},
                search=None,
                sort="cluster_name",
                sort_desc=descending,
                cursor=cursor,
                page_size=4,
                with_count=False,
            )
            seen.extend(item.id for item in page.items)
            order.extend(item.openshift.cluster_name for item in page.items)
            if not page.has_more or page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert len(seen) == len(set(seen)) == 15
        # Nulls sort before every string ascending, after them descending.
        ranked = [(value is not None, value or "") for value in order]
        assert ranked == sorted(ranked, reverse=descending)


@pytest.mark.parametrize("sort", ["updated_at", "last_seen_at"])
async def test_sorting_by_a_datetime_field_pages_without_gaps(
    mongo_holder: MongoClientHolder, sort: str
) -> None:
    """Regression: the cursor carried a real datetime against an ISO-string
    field, and `$gt` never compares across types, so page two was empty
    (ADR-0006). Found live 2026-09-13 with 30 seeded servers.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for index in range(12):
        server = _make_server(index)
        server.last_seen_at = server.updated_at
        await repo.upsert(server)

    for descending in (False, True):
        seen: list[str] = []
        cursor: str | None = None
        while True:
            page = await repo.list_page(
                filters={},
                search=None,
                sort=sort,
                sort_desc=descending,
                cursor=cursor,
                page_size=5,
                with_count=False,
            )
            seen.extend(item.id for item in page.items)
            if not page.has_more or page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert len(seen) == len(set(seen)) == 12


async def test_unknown_sort_field_raises(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    with pytest.raises(UnknownSortFieldError):
        await repo.list_page(
            filters={},
            search=None,
            sort="not_a_real_sort",
            sort_desc=False,
            cursor=None,
            page_size=10,
            with_count=False,
        )


async def test_stale_cursor_after_filter_change_is_rejected(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(5):
        await repo.upsert(_make_server(i, site_id="tlv"))

    page = await repo.list_page(
        filters={"site_id": "tlv"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=2,
        with_count=False,
    )
    assert page.next_cursor is not None

    with pytest.raises(CursorFilterMismatchError):
        await repo.list_page(
            filters={"site_id": "nyc"},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=page.next_cursor,
            page_size=2,
            with_count=False,
        )


async def test_garbage_cursor_is_rejected(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    with pytest.raises(CursorInvalidError):
        await repo.list_page(
            filters={},
            search=None,
            sort="name",
            sort_desc=False,
            cursor="not-a-real-cursor",
            page_size=10,
            with_count=False,
        )


async def test_count_reflects_filters(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(4):
        await repo.upsert(_make_server(i, health=HealthSeverity.CRITICAL))
    for i in range(4, 6):
        await repo.upsert(_make_server(i, health=HealthSeverity.HEALTHY))

    count = await repo.count({"health.overall": HealthSeverity.CRITICAL.value})
    assert count == 4


async def test_search_query_uses_index_scan_not_collection_scan(
    mongo_holder: MongoClientHolder,
) -> None:
    """The entire point of the `search_tokens` multikey index: an anchored,
    escaped-prefix search must be an IXSCAN, never a COLLSCAN, at any
    collection size a real deployment could reach.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(30):
        await repo.upsert(_make_server(i, name=f"ocp-dell-worker-{i:03d}"))

    mongo_query = build_search_query("ocp-dell")
    explain = await mongo_holder.db["servers"].find(mongo_query).explain()
    explain_str = json.dumps(explain)

    assert "COLLSCAN" not in explain_str
    assert "IXSCAN" in explain_str


async def test_site_breakdown_groups_by_installation_type(
    mongo_holder: MongoClientHolder,
) -> None:
    """The landing page's fleet-wide UPI/hosted cards are summed from this
    dimension, so it has to survive the `$group` — not just be present.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(3):
        await repo.upsert(_make_server(i, site_id="tlv", installation_type=InstallationType.UPI))
    for i in range(3, 5):
        await repo.upsert(
            _make_server(i, site_id="tlv", installation_type=InstallationType.HOSTED_CLUSTER)
        )
    await repo.upsert(
        _make_server(
            5,
            site_id="nyc",
            installation_type=InstallationType.UPI,
            health=HealthSeverity.CRITICAL,
        )
    )

    rows = await repo.site_breakdown()

    counts = {
        (row.site_id, row.installation_type): row.count
        for row in rows
        if row.site_id in {"tlv", "nyc"}
    }
    assert counts == {
        ("tlv", InstallationType.UPI.value): 3,
        ("tlv", InstallationType.HOSTED_CLUSTER.value): 2,
        ("nyc", InstallationType.UPI.value): 1,
    }
    # The new key splits rows rather than replacing an existing dimension:
    # the critical NYC server is still reported as critical.
    critical = [row for row in rows if row.health == HealthSeverity.CRITICAL.value]
    assert [(row.site_id, row.installation_type) for row in critical] == [
        ("nyc", InstallationType.UPI.value)
    ]


async def test_upsert_with_revision_check_succeeds_when_revision_matches(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    server = _make_server(600)
    await repo.upsert(server)

    server.name = "renamed"
    server.revision += 1
    await repo.upsert_with_revision_check(server, expected_revision=1)

    fetched = await repo.get_by_id(server.id)
    assert fetched is not None
    assert fetched.name == "renamed"
    assert fetched.revision == 2


async def test_upsert_with_revision_check_rejects_a_concurrent_writer(
    mongo_holder: MongoClientHolder,
) -> None:
    """The compare-and-set race: two callers read revision 1, both mutate,
    and the second to write must lose rather than clobber the first.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    server = _make_server(601)
    await repo.upsert(server)

    first_writer = server.model_copy(deep=True)
    second_writer = server.model_copy(deep=True)

    first_writer.name = "first-writer-won"
    first_writer.revision += 1
    await repo.upsert_with_revision_check(first_writer, expected_revision=1)

    second_writer.name = "second-writer-should-lose"
    second_writer.revision += 1
    with pytest.raises(RevisionConflictError) as exc_info:
        await repo.upsert_with_revision_check(second_writer, expected_revision=1)
    assert exc_info.value.details["current_revision"] == 2

    fetched = await repo.get_by_id(server.id)
    assert fetched is not None
    assert fetched.name == "first-writer-won"
    assert fetched.revision == 2


async def test_upsert_with_revision_check_on_a_deleted_document_raises_not_found(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    server = _make_server(602)
    # Deliberately never inserted — nothing for the filter to match, and
    # no document to report a "current" revision from either.
    with pytest.raises(NotFoundError):
        await repo.upsert_with_revision_check(server, expected_revision=1)


async def test_a_document_stored_with_the_retired_info_severity_still_loads(
    mongo_holder: MongoClientHolder,
) -> None:
    """`INFO` left `HealthSeverity` on 2026-09-13. A document written before that
    must still decode (ADR-0026's rule: write the old shape, read it back), and
    it decodes as HEALTHY — the only positive verdict left.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    server = _make_server(0)
    doc = server.model_dump(by_alias=True, mode="json")
    doc["health"]["overall"] = "INFO"
    doc["health"]["storage"] = "INFO"
    await mongo_holder.db["servers"].insert_one(doc)

    loaded = await repo.get_by_id(server.id)

    assert loaded is not None
    assert loaded.health.overall is HealthSeverity.HEALTHY
    assert loaded.health.storage is HealthSeverity.HEALTHY
