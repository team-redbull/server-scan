"""
Maps engine results onto the small embedded models a `Server` document persists.

`ClassificationResult` -> `Classification`, `HealthState` -> `Health`.

A dedicated mapping module rather than inlining this in `ingest.py`: the
engines' result types carry engine-internal detail (conflicts, per-leaf
evidence, shadowed/suppressed families) that's useful for an audit trail
or a "why is this WARNING" UI panel later, but the embedded `Server.
classification`/`Server.health` fields are deliberately small summaries —
keeping the mapping in one place means that summary boundary is enforced
in exactly one spot, not re-decided at every call site.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import HEALTH_SEVERITY_RANK
from app.domain.models.classification import Classification
from app.domain.models.health import Health, HealthReason
from app.domain.models.health_policy import HealthPolicy
from app.domain.services.classification import ClassificationResult
from app.domain.services.health.evaluate import CATEGORIES, HealthState


def classification_from_result(
    result: ClassificationResult, *, previous_version: int
) -> Classification:
    """
    Summarize a `ClassificationResult` into the embedded `Classification` a server persists.

    Args:
        result (ClassificationResult): The engine's full classification result.
        previous_version (int): The server's classification version before this run.

    Returns:
        Classification: The persisted summary, with `classification_version` incremented.
    """
    return Classification(
        installation_type=result.installation_type,
        matched_rule_id=result.rule_id,
        matched_pattern=result.matched_pattern,
        matched_field=result.matched_field,
        classified_at=result.classified_at,
        classification_version=previous_version + 1,
    )


MAX_HEALTH_REASONS = 10


def _health_reasons(state: HealthState) -> list[HealthReason]:
    """
    Build the capped, worst-first reasons of an evaluation, without evidence.

    Args:
        state (HealthState): The engine's full evaluation result.

    Returns:
        list[HealthReason]: Active policies, worst severity first.
    """
    active = sorted(
        (e for e in state.evaluations if e.active),
        key=lambda e: (-HEALTH_SEVERITY_RANK[e.severity], e.policy_key),
    )
    return [
        HealthReason(
            policy_key=e.policy_key,
            policy_name=e.policy_name,
            category=e.category,
            severity=e.severity,
            message=e.message,
        )
        for e in active[:MAX_HEALTH_REASONS]
    ]


def health_from_state(state: HealthState) -> Health:
    """
    Summarize a `HealthState` into the embedded `Health` a server persists.

    Args:
        state (HealthState): The engine's full evaluation result.

    Returns:
        Health: The persisted per-category severities and overall status.
    """
    severities = {cat: state.categories[cat].severity for cat in CATEGORIES}
    return Health(
        overall=state.overall,
        memory=severities["memory"],
        storage=severities["storage"],
        network=severities["network"],
        connectivity=severities["connectivity"],
        power=severities["power"],
        gpu=severities["gpu"],
        bmc=severities["bmc"],
        evaluated_at=state.evaluated_at,
        active_policy_keys=sorted(e.policy_key for e in state.evaluations if e.active),
        reasons=_health_reasons(state),
    )


def health_change_data(
    previous: Health, state: HealthState, policies: list[HealthPolicy]
) -> dict[str, Any]:
    """
    Build the `HEALTH_CHANGED` event payload: the verdict change and why.

    Kept out of `Server` so the stored shape is unchanged; callers hold the
    `HealthState` they already computed. Evidence is left out (can be large).

    Args:
        previous (Health): The server's health before this evaluation.
        state (HealthState): The engine result for the new evaluation.
        policies (list[HealthPolicy]): The policy set, to describe a legacy
            resolved policy the state no longer carries (else its key).

    Returns:
        dict[str, Any]: `from`, `to`, and `from_reasons`/`to_reasons`, each
            worst first and capped. A legacy `previous` (no stored reasons)
            is rebuilt from its active keys and the policy list, message null.
    """
    from_reasons: list[dict[str, Any]] = [r.model_dump(mode="json") for r in previous.reasons]
    if not from_reasons:
        by_key = {p.policy_key: p for p in policies}
        for k in previous.active_policy_keys[:MAX_HEALTH_REASONS]:
            p = by_key.get(k)
            from_reasons.append(
                {
                    "policy_key": k,
                    "policy_name": p.name if p else k,
                    "category": p.category if p else None,
                    "severity": p.severity.value if p else None,
                    "message": None,
                }
            )
    return {
        "from": previous.overall.value,
        "to": state.overall.value,
        "from_reasons": from_reasons,
        "to_reasons": [r.model_dump(mode="json") for r in _health_reasons(state)],
    }
