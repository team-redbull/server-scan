"""
Integration tests for `MongoClassificationRuleRepository` against the live
dev Mongo: CRUD round-trip, the unique `name` index, `list_all`'s resolution
order, and that the resolution-order query is an IXSCAN.
"""

from __future__ import annotations

import json

import pytest
from pymongo.errors import DuplicateKeyError

from app.application.services import bootstrap
from app.application.services.bootstrap import ensure_default_classification_rules
from app.domain.enums import InstallationType
from app.domain.models.classification_rule import ClassificationRule, RuleScope
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.site import site_catalog
from app.errors import RuleScopeInvalidError
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
    default_system_rules,
)
from app.infrastructure.mongodb.indexes import CLASSIFICATION_RULES_COLLECTION
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

SITES = site_catalog("")
ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)

pytestmark = pytest.mark.integration


def _make_rule(
    name: str,
    *,
    priority: int = 200,
    order: int = 0,
    enabled: bool = True,
    source: str = "GLOBAL_CUSTOM",
    scope: RuleScope | None = None,
    pattern: str = r"^ocp-.*",
    field: str = "name",
    installation_type: InstallationType = InstallationType.HOSTED_CLUSTER,
) -> ClassificationRule:
    now = utcnow()
    return ClassificationRule(
        _id=new_id("classification_rule"),
        name=name,
        installation_type=installation_type,
        scope=scope or RuleScope(),
        field=field,
        pattern=pattern,
        source=source,
        priority=priority,
        order=order,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


async def test_insert_and_get_by_id_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    rule = _make_rule("rule-round-trip")

    await repo.upsert(rule)
    fetched = await repo.get_by_id(rule.id)

    assert fetched is not None
    assert fetched.id == rule.id
    assert fetched.name == rule.name
    assert fetched.pattern == rule.pattern


async def test_get_by_id_missing_returns_none(mongo_holder: MongoClientHolder) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    assert await repo.get_by_id("crul_does_not_exist") is None


async def test_get_by_name_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    rule = _make_rule("rule-by-name")
    await repo.upsert(rule)

    fetched = await repo.get_by_name("rule-by-name")
    assert fetched is not None
    assert fetched.id == rule.id

    assert await repo.get_by_name("does-not-exist") is None


async def test_delete_returns_true_when_present_false_when_absent(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    rule = _make_rule("rule-to-delete")
    await repo.upsert(rule)

    assert await repo.delete(rule.id) is True
    assert await repo.get_by_id(rule.id) is None
    assert await repo.delete(rule.id) is False


async def test_duplicate_name_raises_duplicate_key_error(mongo_holder: MongoClientHolder) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    first = _make_rule("shared-name")
    second = _make_rule("shared-name")

    await repo.upsert(first)
    with pytest.raises(DuplicateKeyError):
        await repo.upsert(second)


async def test_list_all_enabled_only_excludes_disabled(mongo_holder: MongoClientHolder) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    await repo.upsert(_make_rule("enabled-rule", enabled=True))
    await repo.upsert(_make_rule("disabled-rule", enabled=False))

    rules = await repo.list_all(enabled_only=True)

    assert [r.name for r in rules] == ["enabled-rule"]


async def test_list_all_sorts_by_priority_desc_order_asc_id_asc(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    low_priority = _make_rule("low-priority", priority=100)
    high_priority = _make_rule("high-priority", priority=300)
    mid_priority_first_order = _make_rule("mid-order-0", priority=200, order=0)
    mid_priority_second_order = _make_rule("mid-order-1", priority=200, order=1)

    # Inserted out of order, to prove the sort drives the result.
    for rule in (mid_priority_second_order, low_priority, high_priority, mid_priority_first_order):
        await repo.upsert(rule)

    rules = await repo.list_all()

    assert [r.name for r in rules] == [
        "high-priority",
        "mid-order-0",
        "mid-order-1",
        "low-priority",
    ]


async def test_default_system_rules_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    for rule in default_system_rules(SITES):
        await repo.upsert(rule)

    rules = await repo.list_all(enabled_only=True)
    assert len(rules) == len(default_system_rules(SITES))
    # A mangled pattern silently misclassifies rather than erroring.
    stored = {r.name: r for r in rules}
    for original in default_system_rules(SITES):
        assert stored[original.name].pattern == original.pattern
        assert stored[original.name].installation_type == original.installation_type


async def test_declared_indexes_exist(mongo_holder: MongoClientHolder) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    await repo.upsert(_make_rule("index-probe"))  # ensure collection exists

    cursor = await mongo_holder.db[CLASSIFICATION_RULES_COLLECTION].list_indexes()
    index_docs = await cursor.to_list(length=None)
    index_names = {doc["name"] for doc in index_docs}

    assert "uniq_name" in index_names
    assert "enabled_priority_order_id" in index_names
    assert "scope_site_id" in index_names
    assert "scope_vendor" in index_names
    assert "scope_manager_type" in index_names


async def test_load_enabled_rules_in_resolution_order_uses_index_scan(
    mongo_holder: MongoClientHolder,
) -> None:
    """The `enabled_priority_order_id` index: `{enabled: true}` sorted
    `(priority DESC, order ASC, _id ASC)` must be an IXSCAN, never a blocking SORT.
    """
    repo = MongoClassificationRuleRepository(mongo_holder)
    for i in range(20):
        await repo.upsert(_make_rule(f"rule-{i:03d}", priority=100 + i, enabled=i % 2 == 0))

    collection = mongo_holder.db[CLASSIFICATION_RULES_COLLECTION]
    explain = await (
        collection.find({"enabled": True})
        .sort([("priority", -1), ("order", 1), ("_id", 1)])
        .explain()
    )
    explain_str = json.dumps(explain)

    assert "COLLSCAN" not in explain_str
    assert "IXSCAN" in explain_str
    assert '"stage": "SORT"' not in explain_str


async def test_bootstrap_resyncs_a_stale_system_rule_but_keeps_its_enabled_flag(
    mongo_holder: MongoClientHolder,
) -> None:
    """A default rule's pattern is generated from `SiteCode`, so renaming a
    site changes it. Seeding only when missing would leave every existing
    deployment matching hostnames for sites that no longer exist.
    """
    repo = MongoClassificationRuleRepository(mongo_holder)
    generated = default_system_rules(SITES)[0]
    stale = generated.model_copy(
        update={"pattern": r"^ocp4-(one|two|three|four|five)-\d+$", "enabled": False}
    )
    await repo.upsert(stale)

    written = await ensure_default_classification_rules(repo, SITES, engine=ENGINE)

    assert written >= 1
    stored = await repo.get_by_name(generated.name)
    assert stored is not None
    assert stored.pattern == generated.pattern
    assert stored.id == stale.id  # same document, not a second one
    assert stored.enabled is False  # the one field an admin owns survives
    assert stored.revision == stale.revision + 1


async def test_bootstrap_is_a_no_op_once_the_rules_match_the_code(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoClassificationRuleRepository(mongo_holder)
    await ensure_default_classification_rules(repo, SITES, engine=ENGINE)
    assert await ensure_default_classification_rules(repo, SITES, engine=ENGINE) == 0


async def test_bootstrap_rejects_a_malformed_system_rule_at_startup(
    mongo_holder: MongoClientHolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`validate_rule_write` now runs over every shipped default before it
    is written (C12) — a defect in a default's own definition must fail
    the startup, not surface later as a silent classification miss.
    """
    repo = MongoClassificationRuleRepository(mongo_holder)
    broken = _make_rule("broken-system-rule", source="GLOBAL_CUSTOM", priority=999)
    monkeypatch.setattr(bootstrap, "default_system_rules", lambda sites: [broken])

    with pytest.raises(RuleScopeInvalidError):
        await ensure_default_classification_rules(repo, SITES, engine=ENGINE)
