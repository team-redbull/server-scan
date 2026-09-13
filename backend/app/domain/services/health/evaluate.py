"""Health policy resolution and evaluation.

This is where the platform spec's override requirement is actually solved:
a site policy must be able to *replace* a global default, defaults must be
disable-able per scope, and many unrelated policies must still fire
independently. See `app.domain.models.health_policy`'s module docstring
for the `policy_key` mechanism this implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import groupby
from typing import Any

from app.domain.enums.core import HEALTH_SEVERITY_RANK, HealthSeverity
from app.domain.models.health_policy import HealthPolicy
from app.domain.services.health.conditions import evaluate_condition, leaf_metrics
from app.domain.services.health.metrics import MetricRegistry
from app.domain.services.health.template import render_template
from app.utils.timeutil import utcnow

ENGINE_VERSION = 1
CATEGORIES = ("cpu", "memory", "storage", "network", "connectivity", "power", "gpu")


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The result of evaluating one `policy_key` family's winning policy."""

    policy_id: str
    policy_key: str
    policy_name: str
    category: str
    severity: HealthSeverity
    active: bool
    message: str | None
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ShadowedEntry:
    """A policy that lost its `policy_key` family and was not evaluated."""

    policy_id: str
    policy_key: str
    shadowed_by: str


@dataclass(frozen=True, slots=True)
class SuppressedEntry:
    """A family winner in `SUPPRESS` mode: an explicit, auditable no-op rather than silence."""

    policy_id: str
    policy_key: str
    policy_name: str


@dataclass(frozen=True, slots=True)
class CategoryHealth:
    """The rolled-up severity for one health category (cpu, memory, storage, ...)."""

    severity: HealthSeverity
    active_count: int
    evaluated_count: int


@dataclass(frozen=True, slots=True)
class HealthState:
    """The full result of evaluating a server's health: overall, per-category and per-policy."""

    overall: HealthSeverity
    categories: dict[str, CategoryHealth]
    evaluations: list[Evaluation]
    shadowed: list[ShadowedEntry]
    suppressed: list[SuppressedEntry]
    engine_version: int = ENGINE_VERSION
    evaluated_at: datetime = field(default_factory=utcnow)


def _family_sort_key(policy: HealthPolicy) -> tuple[int, int, str]:
    # specificity DESC, priority DESC, id ASC
    return (-policy.scope.specificity(), -policy.priority, policy.id)


def resolve_families(
    policies: list[HealthPolicy], *, vendor: str, manager_type: str | None, site_id: str | None
) -> tuple[dict[str, HealthPolicy], list[ShadowedEntry]]:
    """
    Resolve each `policy_key` family to its single winning policy (ADR-0005).

    Disabled policies compete too — that is how a scope switches a default
    off. Winner: highest specificity, then priority, then lowest id.

    Args:
        policies (list[HealthPolicy]): The candidate policies, any order.
        vendor (str): The server's vendor, for scope matching.
        manager_type (str | None): The server's manager type, for scope matching.
        site_id (str | None): The server's site, for scope matching.

    Returns:
        tuple[dict[str, HealthPolicy], list[ShadowedEntry]]: Each family's
            winner, keyed by `policy_key`, plus a record of every policy
            that winner shadowed.
    """
    matching = [
        p
        for p in policies
        if p.scope.matches(vendor=vendor, manager_type=manager_type, site_id=site_id)
    ]
    matching.sort(key=lambda p: p.policy_key)

    winners: dict[str, HealthPolicy] = {}
    shadowed: list[ShadowedEntry] = []
    for key, group_iter in groupby(matching, key=lambda p: p.policy_key):
        family = sorted(group_iter, key=_family_sort_key)
        head, rest = family[0], family[1:]
        winners[key] = head
        shadowed.extend(
            ShadowedEntry(policy_id=p.id, policy_key=key, shadowed_by=head.id) for p in rest
        )
    return winners, shadowed


def evaluate_health(
    facts: dict[str, Any],
    policies: list[HealthPolicy],
    registry: MetricRegistry,
    *,
    vendor: str,
    manager_type: str | None,
    site_id: str | None,
) -> HealthState:
    """
    Evaluate every policy family and roll the results up into overall health.

    Each family's winner (`resolve_families`) is evaluated unless disabled or
    suppressed; the worst active severity wins per category, then overall.

    Args:
        facts (dict[str, Any]): The extracted server facts (see
            `app.domain.services.health.facts.extract_facts`).
        policies (list[HealthPolicy]): The candidate policies, any order.
        registry (MetricRegistry): The registry to resolve conditions and
            evidence metrics against.
        vendor (str): The server's vendor, for scope matching.
        manager_type (str | None): The server's manager type, for scope matching.
        site_id (str | None): The server's site, for scope matching.

    Returns:
        HealthState: The full evaluation result.
    """
    winners, shadowed = resolve_families(
        policies, vendor=vendor, manager_type=manager_type, site_id=site_id
    )

    evaluations: list[Evaluation] = []
    suppressed: list[SuppressedEntry] = []

    for key in sorted(winners):  # deterministic output order
        policy = winners[key]
        if not policy.enabled:
            continue  # a disabled family head means the family contributes nothing
        if policy.mode == "SUPPRESS":
            suppressed.append(
                SuppressedEntry(policy_id=policy.id, policy_key=key, policy_name=policy.name)
            )
            continue

        active = evaluate_condition(policy.condition, facts, registry)
        evidence = {}
        for ev in policy.evidence:
            metric = registry.get(ev.metric)
            evidence[ev.key] = metric.resolver(facts) if metric is not None else None

        evaluations.append(
            Evaluation(
                policy_id=policy.id,
                policy_key=key,
                policy_name=policy.name,
                category=policy.category,
                severity=policy.severity if active else HealthSeverity.HEALTHY,
                active=active,
                message=render_template(policy.message_template, evidence) if active else None,
                evidence=evidence,
            )
        )

    categories: dict[str, CategoryHealth] = {}
    for cat in CATEGORIES:
        cat_evals = [e for e in evaluations if e.category == cat]
        active_evals = [e for e in cat_evals if e.active]
        if active_evals:
            severity = max(
                (e.severity for e in active_evals), key=lambda s: HEALTH_SEVERITY_RANK[s]
            )
        elif cat_evals:
            severity = HealthSeverity.HEALTHY
        else:
            severity = HealthSeverity.UNKNOWN
        categories[cat] = CategoryHealth(
            severity=severity, active_count=len(active_evals), evaluated_count=len(cat_evals)
        )

    overall = max((c.severity for c in categories.values()), key=lambda s: HEALTH_SEVERITY_RANK[s])

    return HealthState(
        overall=overall,
        categories=categories,
        evaluations=evaluations,
        shadowed=shadowed,
        suppressed=suppressed,
    )


__all__ = [
    "CategoryHealth",
    "Evaluation",
    "HealthState",
    "ShadowedEntry",
    "SuppressedEntry",
    "evaluate_health",
    "leaf_metrics",
    "resolve_families",
]
