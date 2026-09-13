"""The `managers` collection after the 2026-09-13 field removal (ADR-0026 rule)."""

from __future__ import annotations

import pytest

from app.domain.enums import ManagerType
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import MANAGERS_COLLECTION, ensure_indexes
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository

pytestmark = pytest.mark.integration


async def test_a_document_written_before_the_field_removal_still_loads(
    mongo_holder: MongoClientHolder,
) -> None:
    """Old documents carry `site_id`, `parent_manager_id`, `bmc_credential_ref`
    and `metadata`; the model ignores them, and `upsert` (a `$set`) leaves
    them in place — harmless, and it must not blank `last_run` either.
    """
    old_shape = {
        "_id": "mgr_ucs_central",
        "name": "ucs-central",
        "type": "UCS_CENTRAL",
        "site_id": None,
        "parent_manager_id": None,
        "endpoint": "https://central.example",
        "enabled": True,
        "bmc_credential_ref": None,
        "metadata": {},
        "audit": {
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            "created_by": None,
            "updated_by": None,
            "revision": 1,
        },
        "last_run": None,
    }
    await mongo_holder.db[MANAGERS_COLLECTION].insert_one(old_shape)
    repo = MongoManagerRepository(mongo_holder)

    loaded = await repo.get_by_id("mgr_ucs_central")

    assert loaded is not None
    assert loaded.type is ManagerType.UCS_CENTRAL
    assert loaded.endpoint == "https://central.example"
    assert not hasattr(loaded, "parent_manager_id")

    await repo.upsert(loaded)
    assert await repo.get_by_id("mgr_ucs_central") is not None


async def test_the_retired_parent_index_is_dropped_on_startup(
    mongo_holder: MongoClientHolder,
) -> None:
    """A deployed database keeps an index forever unless `RETIRED_INDEXES` names it."""
    collection = mongo_holder.db[MANAGERS_COLLECTION]
    await collection.create_index("parent_manager_id", name="parent_manager_id")
    assert "parent_manager_id" in await collection.index_information()

    await ensure_indexes(mongo_holder.db)

    assert "parent_manager_id" not in await collection.index_information()
