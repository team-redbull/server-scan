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

from app.domain.models.classification import Classification
from app.domain.models.health import Health
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
        cpu=severities["cpu"],
        memory=severities["memory"],
        storage=severities["storage"],
        network=severities["network"],
        connectivity=severities["connectivity"],
        power=severities["power"],
        gpu=severities["gpu"],
        evaluated_at=state.evaluated_at,
        active_policy_keys=sorted(e.policy_key for e in state.evaluations if e.active),
    )
