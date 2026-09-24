"""The `membership_runs` collection — ADR-0029's 2026-09-24 update."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.models.openshift import MembershipRun
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.membership_run_repository import MongoMembershipRunRepository

pytestmark = pytest.mark.integration


def _run(**overrides: object) -> MembershipRun:
    base: dict[str, object] = {
        "kind": "nodes",
        "reported_by": "hc-tlv-01",
        "finished_at": datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        "duration_seconds": 5.0,
        "observed": 50,
        "matched": 48,
        "unmatched": 2,
        "partial": True,
    }
    base.update(overrides)
    return MembershipRun.model_validate(base)


async def test_a_run_round_trips(mongo_holder: MongoClientHolder) -> None:
    repo = MongoMembershipRunRepository(mongo_holder)

    await repo.record_run(_run())

    [loaded] = await repo.list_all()
    assert loaded.reported_by == "hc-tlv-01"
    assert loaded.unmatched == 2
    assert loaded.partial is True


async def test_a_later_run_replaces_the_stored_one(mongo_holder: MongoClientHolder) -> None:
    repo = MongoMembershipRunRepository(mongo_holder)

    await repo.record_run(_run(unmatched=2, partial=True))
    await repo.record_run(_run(unmatched=0, partial=False))

    [loaded] = await repo.list_all()
    assert loaded.unmatched == 0
    assert loaded.partial is False


async def test_a_nodes_cluster_and_an_agents_mce_of_the_same_name_do_not_collide(
    mongo_holder: MongoClientHolder,
) -> None:
    """Keyed on `kind:reported_by`, not `reported_by` alone."""
    repo = MongoMembershipRunRepository(mongo_holder)

    await repo.record_run(_run(kind="nodes", reported_by="tlv", unmatched=1))
    await repo.record_run(_run(kind="agents", reported_by="tlv", unmatched=9))

    runs = {(r.kind, r.reported_by): r.unmatched for r in await repo.list_all()}
    assert runs == {("nodes", "tlv"): 1, ("agents", "tlv"): 9}
