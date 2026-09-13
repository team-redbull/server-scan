"""
Integration tests for `MongoHealthPolicyRepository` against the live dev
Mongo: CRUD round-trip, the unique `name` index, and that the family-
resolution load order is an IXSCAN.
"""

from __future__ import annotations

import json

import pytest
from pymongo.errors import DuplicateKeyError

from app.application.services import bootstrap
from app.application.services.bootstrap import ensure_default_health_policies
from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import HealthPolicy, PolicyScope
from app.domain.services.health.conditions import Condition
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.domain.services.health.metrics import build_default_registry
from app.errors import AppError
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.indexes import HEALTH_POLICIES_COLLECTION
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

REGISTRY = build_default_registry()


def _make_policy(
    name: str,
    *,
    policy_key: str | None = None,
    priority: int = 200,
    order: int = 0,
    enabled: bool = True,
    source: str = "GLOBAL_CUSTOM",
    scope: PolicyScope | None = None,
    category: str = "connectivity",
    severity: HealthSeverity = HealthSeverity.WARNING,
) -> HealthPolicy:
    now = utcnow()
    policy_id = new_id("health_policy")
    return HealthPolicy(
        id=policy_id,
        name=name,
        policy_key=policy_key or policy_id,
        category=category,
        severity=severity,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        message_template="{down} down",
        scope=scope or PolicyScope(),
        source=source,
        priority=priority,
        order=order,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


async def test_insert_and_get_by_id_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    policy = _make_policy("policy-round-trip")

    await repo.upsert(policy)
    fetched = await repo.get_by_id(policy.id)

    assert fetched is not None
    assert fetched.id == policy.id
    assert fetched.name == policy.name
    assert fetched.policy_key == policy.policy_key


async def test_get_by_id_missing_returns_none(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    assert await repo.get_by_id("hpol_does_not_exist") is None


async def test_get_by_name_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    policy = _make_policy("policy-by-name")
    await repo.upsert(policy)

    fetched = await repo.get_by_name("policy-by-name")
    assert fetched is not None
    assert fetched.id == policy.id

    assert await repo.get_by_name("does-not-exist") is None


async def test_delete_returns_true_when_present_false_when_absent(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    policy = _make_policy("policy-to-delete")
    await repo.upsert(policy)

    assert await repo.delete(policy.id) is True
    assert await repo.get_by_id(policy.id) is None
    assert await repo.delete(policy.id) is False


async def test_duplicate_name_raises_duplicate_key_error(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    first = _make_policy("shared-name")
    second = _make_policy("shared-name")

    await repo.upsert(first)
    with pytest.raises(DuplicateKeyError):
        await repo.upsert(second)


async def test_list_all_enabled_only_excludes_disabled(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    await repo.upsert(_make_policy("enabled-policy", enabled=True))
    await repo.upsert(_make_policy("disabled-policy", enabled=False))

    policies = await repo.list_all(enabled_only=True)

    assert [p.name for p in policies] == ["enabled-policy"]


async def test_list_all_returns_every_policy_regardless_of_enabled(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    await repo.upsert(_make_policy("enabled-policy", enabled=True))
    await repo.upsert(_make_policy("disabled-policy", enabled=False))

    policies = await repo.list_all()

    assert {p.name for p in policies} == {"enabled-policy", "disabled-policy"}


async def test_default_system_policies_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    for policy in default_system_policies():
        await repo.upsert(policy)

    policies = await repo.list_all(enabled_only=True)
    # Against the seeded set itself, not a copy of it: this test is about the
    # round trip through Mongo, and a hardcoded list turns every new default
    # policy into a failure here rather than in the tests that own them.
    expected = default_system_policies()
    assert len(policies) == len(expected)
    assert {p.policy_key for p in policies} == {p.policy_key for p in expected}
    assert all(p.system for p in policies)
    assert all(p.source == "SYSTEM_DEFAULT" for p in policies)


async def test_declared_indexes_exist(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    await repo.upsert(_make_policy("index-probe"))  # ensure collection exists

    cursor = await mongo_holder.db[HEALTH_POLICIES_COLLECTION].list_indexes()
    index_docs = await cursor.to_list(length=None)
    index_names = {doc["name"] for doc in index_docs}

    assert "uniq_name" in index_names
    assert "enabled_policy_key_priority_order_id" in index_names
    assert "policy_key" in index_names
    assert "category" in index_names
    assert "scope_site_id" in index_names


async def test_family_resolution_load_order_uses_index_scan(
    mongo_holder: MongoClientHolder,
) -> None:
    """The `enabled_policy_key_priority_order_id` index: `{enabled: true}` sorted
    `(policy_key ASC, priority DESC, order ASC, _id ASC)` must be an IXSCAN,
    never a blocking SORT.
    """
    repo = MongoHealthPolicyRepository(mongo_holder)
    for i in range(20):
        await repo.upsert(
            _make_policy(
                f"policy-{i:03d}",
                policy_key=f"key-{i % 4}",
                priority=200 + i,
                enabled=i % 2 == 0,
            )
        )

    collection = mongo_holder.db[HEALTH_POLICIES_COLLECTION]
    explain = await (
        collection.find({"enabled": True})
        .sort([("policy_key", 1), ("priority", -1), ("order", 1), ("_id", 1)])
        .explain()
    )
    explain_str = json.dumps(explain)

    assert "COLLSCAN" not in explain_str
    assert "IXSCAN" in explain_str
    assert '"stage": "SORT"' not in explain_str


async def test_bootstrap_resyncs_a_stale_system_policy_but_keeps_its_enabled_flag(
    mongo_holder: MongoClientHolder,
) -> None:
    """Mirrors the classification-rule bootstrap re-sync test: a system
    policy's definition can drift from what code now generates (C11), and
    there is no editor UI left to notice or correct that by hand.
    """
    repo = MongoHealthPolicyRepository(mongo_holder)
    generated = default_system_policies()[0]
    stale = generated.model_copy(update={"severity": HealthSeverity.MAJOR, "enabled": False})
    await repo.upsert(stale)

    written = await ensure_default_health_policies(repo, registry=REGISTRY)

    assert written >= 1
    stored = await repo.get_by_name(generated.name)
    assert stored is not None
    assert stored.severity == generated.severity
    assert stored.id == stale.id  # same document, not a second one
    assert stored.enabled is False  # the one field an admin owns survives
    assert stored.revision == stale.revision + 1


async def test_a_pre_adr_0030_scalar_manager_type_document_still_loads(
    mongo_holder: MongoClientHolder,
) -> None:
    """Every document written before ADR-0030 carries `scope.manager_type`
    (null, or a single string) rather than `scope.manager_types`.
    """
    repo = MongoHealthPolicyRepository(mongo_holder)
    for policy, legacy in (
        (_make_policy("legacy-null-scope"), None),
        (_make_policy("legacy-string-scope"), "UCS_CENTRAL"),
    ):
        doc = policy.model_dump(mode="json", by_alias=True)
        doc["scope"] = {"site_id": None, "vendor": None, "manager_type": legacy}
        await mongo_holder.db[HEALTH_POLICIES_COLLECTION].insert_one(doc)

    null_scoped = await repo.get_by_name("legacy-null-scope")
    string_scoped = await repo.get_by_name("legacy-string-scope")
    assert null_scoped is not None and null_scoped.scope.manager_types == []
    assert string_scoped is not None and string_scoped.scope.manager_types == ["UCS_CENTRAL"]
    assert string_scoped.scope.matches(vendor="cisco", manager_type="UCS_CENTRAL", site_id=None)


async def test_bootstrap_resyncs_a_default_stored_with_the_old_unscoped_shape(
    mongo_holder: MongoClientHolder,
) -> None:
    """A deployed database holds the fabric defaults with `scope.manager_type:
    null`; startup must give them ADR-0030's collector scope, not keep the
    stored one.
    """
    repo = MongoHealthPolicyRepository(mongo_holder)
    generated = next(p for p in default_system_policies() if p.scope.manager_types)
    doc = generated.model_dump(mode="json", by_alias=True)
    doc["scope"] = {"site_id": None, "vendor": None, "manager_type": None}
    await mongo_holder.db[HEALTH_POLICIES_COLLECTION].insert_one(doc)

    assert await ensure_default_health_policies(repo, registry=REGISTRY) >= 1

    stored = await repo.get_by_name(generated.name)
    assert stored is not None
    assert stored.id == generated.id
    assert stored.scope.manager_types == generated.scope.manager_types


async def test_bootstrap_is_a_no_op_once_the_policies_match_the_code(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    await ensure_default_health_policies(repo, registry=REGISTRY)
    assert await ensure_default_health_policies(repo, registry=REGISTRY) == 0


async def test_bootstrap_rejects_a_malformed_system_policy_at_startup(
    mongo_holder: MongoClientHolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`validate_policy_write` now runs over every shipped default before
    it is written (C12) — a defect in a default's own definition must
    fail the startup, not surface later as a silently-wrong evaluation.
    """
    repo = MongoHealthPolicyRepository(mongo_holder)
    # SITE_CUSTOM requires scope.site_id; this one doesn't set it.
    broken = _make_policy("broken-system-policy", source="SITE_CUSTOM", priority=500)
    monkeypatch.setattr(bootstrap, "default_system_policies", lambda: [broken])

    with pytest.raises(AppError):
        await ensure_default_health_policies(repo, registry=REGISTRY)
