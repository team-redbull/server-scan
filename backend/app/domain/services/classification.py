"""Deterministic classification resolution.

Never relies on MongoDB's natural document order — the sort key here
*is* the resolution order, computed in Python from data the caller already
has, so the result is identical regardless of how the rules were stored,
fetched, or reordered by the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.enums import InstallationType, ManagerType, Vendor
from app.domain.models.classification_rule import CLASSIFIABLE_FIELDS, ClassificationRule
from app.domain.ports.regex_engine import RegexEngine, RegexTimeout
from app.utils.timeutil import utcnow

ENGINE_VERSION = 1
_FIELD_VALUE_TRUNCATE = 120


def _extract_field(
    server_name: str,
    server_hostname: str | None,
    server_serial: str | None,
    server_model: str | None,
    server_site_id: str | None,
    field: str,
) -> str | None:
    values = {
        "name": server_name,
        "hostname": server_hostname,
        "serial": server_serial,
        "model": server_model,
        "site_id": server_site_id,
    }
    return values.get(field)


@dataclass(frozen=True, slots=True)
class ClassifiableServer:
    """
    The minimal view of a `Server` the classifier needs.

    Only the `CLASSIFIABLE_FIELDS` plus scope dimensions, so tests need no full `Server`.
    """

    name: str
    vendor: Vendor
    manager_type: ManagerType | None
    site_id: str | None
    hostname: str | None = None
    serial: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ClassificationConflict:
    """A rule tied with the winner on precedence but disagreed on `installation_type`."""

    rule_id: str
    rule_name: str
    installation_type: InstallationType


@dataclass(frozen=True, slots=True)
class RuleTimeoutError:
    """A rule whose pattern exceeded the regex engine's time budget and was skipped."""

    rule_id: str


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """The outcome of resolving one server against a rule set: the winning rule, if any."""

    installation_type: InstallationType
    rule_id: str | None
    rule_name: str | None
    rule_source: str | None
    matched_field: str | None
    matched_pattern: str | None
    matched_value_preview: str | None
    priority: int | None
    specificity: int | None
    conflicts: list[ClassificationConflict]
    errors: list[RuleTimeoutError]
    engine_version: int = ENGINE_VERSION
    classified_at: datetime = field(default_factory=utcnow)


def _sort_key(rule: ClassificationRule) -> tuple[int, int, int, str]:
    # priority DESC, specificity DESC, order ASC, id ASC
    return (-rule.priority, -rule.scope.specificity(), rule.order, rule.id)


def classify(
    server: ClassifiableServer,
    rules: list[ClassificationRule],
    engine: RegexEngine,
) -> ClassificationResult:
    """
    Resolve one server's `InstallationType` against a set of classification rules.

    First match in priority-DESC, specificity-DESC, order-ASC, id-ASC order wins;
    tied rules are still scanned, only to record a `ClassificationConflict`.

    Args:
        server (ClassifiableServer): The server to classify.
        rules (list[ClassificationRule]): The candidate rules, in any order.
        engine (RegexEngine): The regex engine to match rule patterns with.

    Returns:
        ClassificationResult: The winning rule's outcome, or
            `InstallationType.UNCLASSIFIED` with no winner if nothing matched.
    """
    candidates = [
        r
        for r in rules
        if r.enabled
        and not r.stats.quarantined
        and r.scope.matches(
            vendor=server.vendor, manager_type=server.manager_type, site_id=server.site_id
        )
    ]
    candidates.sort(key=_sort_key)

    winner: ClassificationRule | None = None
    winner_value: str | None = None
    conflicts: list[ClassificationConflict] = []
    errors: list[RuleTimeoutError] = []

    for rule in candidates:
        if rule.field not in CLASSIFIABLE_FIELDS:
            continue
        value = _extract_field(
            server.name, server.hostname, server.serial, server.model, server.site_id, rule.field
        )
        if value is None:
            continue

        try:
            match = engine.search(
                rule.pattern,
                value,
                ignore_case=rule.flags.ignore_case,
                multiline=rule.flags.multiline,
                dotall=rule.flags.dotall,
            )
        except RegexTimeout:
            errors.append(RuleTimeoutError(rule_id=rule.id))
            continue
        if match is None:
            continue

        if winner is None:
            winner = rule
            winner_value = value
            continue

        # Past the winner, scan only while precedence is tied, to record
        # a disagreement; `order`/`id` already broke the tie in the sort.
        tied = (rule.priority, rule.scope.specificity()) == (
            winner.priority,
            winner.scope.specificity(),
        )
        if not tied:
            break
        if rule.installation_type != winner.installation_type:
            conflicts.append(
                ClassificationConflict(
                    rule_id=rule.id, rule_name=rule.name, installation_type=rule.installation_type
                )
            )

    if winner is None:
        return ClassificationResult(
            installation_type=InstallationType.UNCLASSIFIED,
            rule_id=None,
            rule_name=None,
            rule_source=None,
            matched_field=None,
            matched_pattern=None,
            matched_value_preview=None,
            priority=None,
            specificity=None,
            conflicts=conflicts,
            errors=errors,
        )

    preview = winner_value[:_FIELD_VALUE_TRUNCATE] if winner_value else None
    return ClassificationResult(
        installation_type=winner.installation_type,
        rule_id=winner.id,
        rule_name=winner.name,
        rule_source=winner.source,
        matched_field=winner.field,
        matched_pattern=winner.pattern,
        matched_value_preview=preview,
        priority=winner.priority,
        specificity=winner.scope.specificity(),
        conflicts=conflicts,
        errors=errors,
    )
