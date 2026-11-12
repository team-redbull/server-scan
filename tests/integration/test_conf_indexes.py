"""`ensure_indexes` on `confs`, a collection another system writes (ADR-0061)."""

from __future__ import annotations

import pytest

from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import CONFS_COLLECTION, ensure_indexes

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", ["conf_id_1", "writer_conf_id"])
async def test_a_writers_unique_index_survives_and_startup_does_not_fail(
    mongo_holder: MongoClientHolder, name: str
) -> None:
    """Same name (spec conflict) or another name (options conflict): kept, never dropped."""
    collection = mongo_holder.db[CONFS_COLLECTION]
    await collection.drop()
    await collection.create_index("conf_id", name=name, unique=True)

    await ensure_indexes(mongo_holder.db)

    info = await collection.index_information()
    assert info[name].get("unique") is True
    await collection.drop()
