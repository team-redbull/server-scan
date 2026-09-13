"""
Idempotent seeding of system-default classification rules and health policies.

Validated the same way a hand-authored write would be.

Both `default_system_rules()` and `default_system_policies()` generate a
fresh random id on every call, so re-running them and unconditionally
upserting would collide on each collection's unique `name` index on every
startup after the first. Seeding is therefore keyed on `name`, and an
existing document keeps its id, its stats and its `enabled` flag — the one
field a system rule/policy allows an admin to change.

Both go one step further: a system rule/policy's *definition* is re-synced
from code on startup when it has drifted. A default rule's pattern is
generated from `SiteCode` (see
`app.infrastructure.mongodb.classification_rule_repository`), so renaming
a site changes it — and seed-only-if-missing would have left every
existing deployment matching hostnames for sites that no longer exist,
silently, with nothing in the UI to suggest the rule was stale. Health
policies carry no site-derived state today, but the same drift is possible
by construction (a future code change to a default's condition/template)
and there is no editor UI left to notice or correct it by hand, so the
same re-sync applies.

Every rule/policy that would be written — seeded or re-synced — is run
through `validate_rule_write`/`validate_policy_write` first. Those were
originally write-time validation for the (now-removed) create/update API
routes; since rules and policies are read-only and bootstrap is the only
remaining writer, running them here is total coverage of what can ever be
persisted, not a narrower substitute — a malformed shipped default now
fails fast at startup instead of silently miscompiling or misevaluating
later.
"""

from __future__ import annotations

import structlog

from app.application.services.classification_service import validate_rule_write
from app.application.services.health_policy_service import validate_policy_write
from app.domain.models.classification_rule import ClassificationRule
from app.domain.models.health_policy import HealthPolicy
from app.domain.ports.regex_engine import RegexEngine
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.domain.services.health.metrics import MetricRegistry
from app.domain.value_objects.site import SiteCatalog
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
    default_system_rules,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)


# What a re-sync carries over from the stored document rather than resets.
_ADMIN_OWNED_RULE_FIELDS = ("id", "enabled", "stats", "created_at", "created_by")
_ADMIN_OWNED_POLICY_FIELDS = ("id", "enabled", "stats", "created_at", "created_by")


def _resynced(stored: ClassificationRule, generated: ClassificationRule) -> ClassificationRule:
    """
    The generated rule, wearing the stored one's admin-owned fields.

    Args:
        stored (ClassificationRule): The rule as it exists in MongoDB.
        generated (ClassificationRule): The same rule as code defines it now.

    Returns:
        ClassificationRule: The definition to store, with the id, enabled
            flag, match stats and creation audit preserved.
    """
    return generated.model_copy(
        update={field: getattr(stored, field) for field in _ADMIN_OWNED_RULE_FIELDS}
        | {"revision": stored.revision + 1, "updated_at": utcnow()}
    )


def _definition_of(rule: ClassificationRule) -> dict[str, object]:
    """
    The parts of a rule that code owns, for comparing stored against generated.

    Args:
        rule (ClassificationRule): Any classification rule.

    Returns:
        dict[str, object]: Its definition, without ids, timestamps or the
            fields an admin may change.
    """
    return rule.model_dump(
        mode="json",
        exclude={*_ADMIN_OWNED_RULE_FIELDS, "revision", "updated_at", "updated_by"},
    )


async def ensure_default_classification_rules(
    repo: MongoClassificationRuleRepository, sites: SiteCatalog, *, engine: RegexEngine
) -> int:
    """
    Seed the system-default rules, re-syncing any whose definition has drifted.

    Each is validated first (`validate_rule_write`): a malformed shipped
    default fails startup, not the first classification that hits it.

    Args:
        repo (MongoClassificationRuleRepository): The rules collection.
        sites (SiteCatalog): The configured site catalog, threaded into
            `default_system_rules` to generate the site-derived patterns.
        engine (RegexEngine): Used only to validate each rule's pattern.

    Returns:
        int: How many rules were written — created plus re-synced. 0 on
            every call after the first, until a default's definition
            changes in code.
    """
    written = 0
    for rule in default_system_rules(sites):
        validate_rule_write(rule, engine)
        stored = await repo.get_by_name(rule.name)
        if stored is None:
            await repo.upsert(rule)
            written += 1
            continue
        if _definition_of(stored) == _definition_of(rule):
            continue
        await repo.upsert(_resynced(stored, rule))
        written += 1
        logger.info("bootstrap.classification_rule_resynced", name=rule.name)
    if written:
        logger.info("bootstrap.classification_rules_seeded", count=written)
    return written


def _resynced_policy(stored: HealthPolicy, generated: HealthPolicy) -> HealthPolicy:
    """
    The generated policy, wearing the stored one's admin-owned fields.

    Args:
        stored (HealthPolicy): The policy as it exists in MongoDB.
        generated (HealthPolicy): The same policy as code defines it now.

    Returns:
        HealthPolicy: The definition to store, with the id, enabled flag,
            match stats and creation audit preserved.
    """
    return generated.model_copy(
        update={field: getattr(stored, field) for field in _ADMIN_OWNED_POLICY_FIELDS}
        | {"revision": stored.revision + 1, "updated_at": utcnow()}
    )


def _definition_of_policy(policy: HealthPolicy) -> dict[str, object]:
    """
    The parts of a policy that code owns, for comparing stored against generated.

    Args:
        policy (HealthPolicy): Any health policy.

    Returns:
        dict[str, object]: Its definition, without ids, timestamps or the
            fields an admin may change.
    """
    return policy.model_dump(
        mode="json",
        exclude={*_ADMIN_OWNED_POLICY_FIELDS, "revision", "updated_at", "updated_by"},
    )


async def ensure_default_health_policies(
    repo: MongoHealthPolicyRepository, *, registry: MetricRegistry
) -> int:
    """
    Seed the system-default health policies, re-syncing any whose definition has drifted.

    Same policy as `ensure_default_classification_rules`; each is validated
    first (`validate_policy_write`).

    Args:
        repo (MongoHealthPolicyRepository): The health policies collection.
        registry (MetricRegistry): Used only to validate each policy's
            condition tree against the known metrics.

    Returns:
        int: How many policies were written — created plus re-synced. 0 on
            every call after the first, until a default's definition
            changes in code.
    """
    written = 0
    for policy in default_system_policies():
        validate_policy_write(policy, registry=registry)
        stored = await repo.get_by_name(policy.name)
        if stored is None:
            await repo.upsert(policy)
            written += 1
            continue
        if _definition_of_policy(stored) == _definition_of_policy(policy):
            continue
        await repo.upsert(_resynced_policy(stored, policy))
        written += 1
        logger.info("bootstrap.health_policy_resynced", name=policy.name)
    if written:
        logger.info("bootstrap.health_policies_seeded", count=written)
    return written
