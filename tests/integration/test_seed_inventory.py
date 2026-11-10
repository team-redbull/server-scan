"""`tools.seed_inventory` against a database an older seeder wrote (ADR-0060)."""

from __future__ import annotations

import pytest
from tools import seed_inventory

from app.domain.enums import ManagerType
from app.domain.models.manager import LEGACY_KUBEVIRT_MANAGER_ID
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import MANAGERS_COLLECTION
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository

pytestmark = pytest.mark.integration


async def test_seeding_replaces_the_shared_kubevirt_manager_with_per_cluster_ones(
    mongo_holder: MongoClientHolder,
) -> None:
    """A fake-only deployment has no KUBEVIRT collector run to delete the old shared manager."""
    await mongo_holder.db[MANAGERS_COLLECTION].insert_one(
        {"_id": LEGACY_KUBEVIRT_MANAGER_ID, "name": "kubevirt", "type": "KUBEVIRT", "enabled": True}
    )

    await seed_inventory._run(count=40, seed=7)

    managers = await MongoManagerRepository(mongo_holder).list_all()
    kubevirt_ids = {m.id for m in managers if m.type is ManagerType.KUBEVIRT}
    assert LEGACY_KUBEVIRT_MANAGER_ID not in kubevirt_ids
    assert kubevirt_ids
    assert all(i.startswith("mgr_kubevirt_") for i in kubevirt_ids)
