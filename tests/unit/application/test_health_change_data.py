"""`health_change_data` says why a verdict changed: from/to reasons worst-first, no evidence."""

from app.application.services.pipeline import MAX_HEALTH_REASONS, health_change_data
from app.domain.enums import HealthSeverity
from app.domain.models.health import Health, HealthReason
from app.domain.models.health_policy import HealthPolicy
from app.domain.services.health.evaluate import Evaluation, HealthState


def _ev(key: str, severity: HealthSeverity, *, active: bool = True) -> Evaluation:
    return Evaluation(
        policy_id=f"id-{key}",
        policy_key=key,
        policy_name=f"Name {key}",
        category="storage",
        severity=severity,
        active=active,
        message=f"{key} fired" if active else None,
        evidence={"big": list(range(1000))},
    )


def _state(overall: HealthSeverity, evals: list[Evaluation]) -> HealthState:
    return HealthState(
        overall=overall, categories={}, evaluations=evals, shadowed=[], suppressed=[]
    )


def test_to_reasons_worst_first_without_evidence() -> None:
    state = _state(
        HealthSeverity.CRITICAL,
        [_ev("a", HealthSeverity.WARNING), _ev("b", HealthSeverity.CRITICAL)],
    )

    data = health_change_data(Health(overall=HealthSeverity.HEALTHY), state, [])

    assert (data["from"], data["to"]) == ("HEALTHY", "CRITICAL")
    assert set(data) == {"from", "to", "from_reasons", "to_reasons"}
    assert [r["policy_key"] for r in data["to_reasons"]] == ["b", "a"]
    assert data["to_reasons"][0] == {
        "policy_key": "b",
        "policy_name": "Name b",
        "category": "storage",
        "severity": "CRITICAL",
        "message": "b fired",
    }
    assert data["from_reasons"] == []


def test_from_reasons_come_from_stored_reasons() -> None:
    stored = HealthReason(
        policy_key="a",
        policy_name="Name a",
        category="storage",
        severity=HealthSeverity.MAJOR,
        message="old message",
    )
    prev = Health(overall=HealthSeverity.MAJOR, active_policy_keys=["a"], reasons=[stored])
    state = _state(HealthSeverity.HEALTHY, [_ev("a", HealthSeverity.MAJOR, active=False)])

    data = health_change_data(prev, state, [])

    assert data["from_reasons"] == [stored.model_dump(mode="json")]
    assert data["to_reasons"] == []


def test_legacy_previous_is_rebuilt_from_keys_and_policies() -> None:
    prev = Health(overall=HealthSeverity.CRITICAL, active_policy_keys=["a", "gone"])
    policy = HealthPolicy.model_construct(
        policy_key="a", name="Policy A", category="power", severity=HealthSeverity.MAJOR
    )

    data = health_change_data(prev, _state(HealthSeverity.HEALTHY, []), [policy])

    assert data["from_reasons"] == [
        {
            "policy_key": "a",
            "policy_name": "Policy A",
            "category": "power",
            "severity": "MAJOR",
            "message": None,
        },
        {
            "policy_key": "gone",
            "policy_name": "gone",
            "category": None,
            "severity": None,
            "message": None,
        },
    ]


def test_reasons_are_capped() -> None:
    evals = [_ev(f"k{i:02d}", HealthSeverity.WARNING) for i in range(MAX_HEALTH_REASONS + 5)]
    data = health_change_data(Health(), _state(HealthSeverity.WARNING, evals), [])
    assert len(data["to_reasons"]) == MAX_HEALTH_REASONS


def test_health_document_without_reasons_loads() -> None:
    health = Health.model_validate({"overall": "CRITICAL", "active_policy_keys": ["a"]})
    assert health.reasons == []
