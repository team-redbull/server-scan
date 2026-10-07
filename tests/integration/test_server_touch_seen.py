"""`MongoServerRepository.touch_seen`: refresh stamps without touching revision (ADR-0044)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.enums import HealthSeverity, InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_SERIAL = "SN-TOUCH-1"


def _server() -> Server:
    now = utcnow()
    return Server(
        _id=new_id("server"),
        name="srv-touch",
        name_normalized=normalize_text("srv-touch"),
        identity=Identity(
            vendor=Vendor.DELL,
            serial=_SERIAL,
            serial_normalized=normalize_text(_SERIAL),
            system_uuid="uuid-touch",
        ),
        classification=Classification(installation_type=InstallationType.UNCLASSIFIED),
        health=Health(overall=HealthSeverity.HEALTHY, evaluated_at=now),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
        listed_at=now,
        revision=3,
    )


async def test_touch_seen_sets_only_the_given_fields_and_keeps_the_revision(
    mongo_holder: MongoClientHolder,
) -> None:
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})
    repo = MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret")
    server = await repo.upsert(_server())
    later = utcnow() + timedelta(hours=3)

    touched = await repo.touch_seen(
        server.id,
        expected_revision=3,
        fields={"last_seen_at": later, "health.evaluated_at": later},
    )
    stored = await repo.get_by_id(server.id)

    assert touched is True
    assert stored is not None
    assert stored.last_seen_at == later
    assert stored.health.evaluated_at == later
    assert stored.listed_at == server.listed_at
    assert stored.revision == 3
    assert stored.updated_at == server.updated_at
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})


async def test_touch_seen_does_nothing_when_the_revision_moved(
    mongo_holder: MongoClientHolder,
) -> None:
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})
    repo = MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret")
    server = await repo.upsert(_server())

    touched = await repo.touch_seen(
        server.id, expected_revision=2, fields={"last_seen_at": utcnow() + timedelta(hours=3)}
    )
    stored = await repo.get_by_id(server.id)

    assert touched is False
    assert stored is not None
    assert stored.last_seen_at == server.last_seen_at
    await mongo_holder.db["servers"].delete_many({"identity.serial": _SERIAL})
