import pytest

from app.domain.services.health.conditions import (
    Condition,
    ConditionValidationError,
    evaluate_condition,
    leaf_metrics,
    validate_condition,
)
from app.domain.services.health.metrics import MetricDef, MetricRegistry, MetricType


def _registry() -> MetricRegistry:
    r = MetricRegistry()
    r.register(
        MetricDef(
            name="down",
            type=MetricType.INT,
            category="connectivity",
            description="",
            resolver=lambda f: f.get("down", 0),
        )
    )
    r.register(
        MetricDef(
            name="up",
            type=MetricType.INT,
            category="connectivity",
            description="",
            resolver=lambda f: f.get("up", 0),
        )
    )
    r.register(
        MetricDef(
            name="drive_healths",
            type=MetricType.LIST_STRING,
            category="storage",
            description="",
            resolver=lambda f: f.get("drive_healths", []),
        )
    )
    r.register(
        MetricDef(
            name="power_state",
            type=MetricType.ENUM,
            category="power",
            description="",
            resolver=lambda f: f.get("power_state"),
            enum_values=("ON", "OFF"),
        )
    )
    return r


REGISTRY = _registry()


class TestScalarOperators:
    @pytest.mark.parametrize(
        ("operator", "facts", "value", "expected"),
        [
            ("EQ", {"down": 2}, 2, True),
            ("EQ", {"down": 1}, 2, False),
            ("NE", {"down": 1}, 2, True),
            ("GT", {"down": 3}, 2, True),
            ("GT", {"down": 2}, 2, False),
            ("GTE", {"down": 2}, 2, True),
            ("LT", {"down": 1}, 2, True),
            ("LTE", {"down": 2}, 2, True),
        ],
    )
    def test_scalar_comparison(
        self, operator: str, facts: dict, value: int, expected: bool
    ) -> None:
        cond = Condition(metric="down", operator=operator, value=value)
        assert evaluate_condition(cond, facts, REGISTRY) is expected


class TestExistence:
    def test_exists_true_when_present(self) -> None:
        cond = Condition(metric="down", operator="EXISTS")
        assert evaluate_condition(cond, {"down": 0}, REGISTRY) is True

    def test_not_exists_true_when_absent(self) -> None:
        # "down" defaults to 0 when absent (0 still "exists"), so NOT_EXISTS
        # needs a metric whose resolver returns None — power_state does.
        cond = Condition(metric="power_state", operator="NOT_EXISTS")
        assert evaluate_condition(cond, {}, REGISTRY) is True


class TestSetOperators:
    def test_in_true_when_member(self) -> None:
        cond = Condition(metric="power_state", operator="IN", value=["ON", "OFF"])
        assert evaluate_condition(cond, {"power_state": "ON"}, REGISTRY) is True

    def test_not_in_true_when_not_member(self) -> None:
        cond = Condition(metric="power_state", operator="NOT_IN", value=["OFF"])
        assert evaluate_condition(cond, {"power_state": "ON"}, REGISTRY) is True


class TestListOperators:
    def test_any_true_when_element_present(self) -> None:
        cond = Condition(metric="drive_healths", operator="ANY", value="FAILED")
        assert evaluate_condition(cond, {"drive_healths": ["OK", "FAILED"]}, REGISTRY) is True

    def test_any_false_when_absent(self) -> None:
        cond = Condition(metric="drive_healths", operator="ANY", value="FAILED")
        assert evaluate_condition(cond, {"drive_healths": ["OK", "OK"]}, REGISTRY) is False

    def test_all_true_when_uniform(self) -> None:
        cond = Condition(metric="drive_healths", operator="ALL", value="OK")
        assert evaluate_condition(cond, {"drive_healths": ["OK", "OK"]}, REGISTRY) is True

    def test_all_false_on_empty_list_vacuous_truth_rejected(self) -> None:
        cond = Condition(metric="drive_healths", operator="ALL", value="OK")
        assert evaluate_condition(cond, {"drive_healths": []}, REGISTRY) is False

    def test_count_gte_with_equals_filter(self) -> None:
        cond = Condition(metric="drive_healths", operator="COUNT_GTE", value=2, equals="FAILED")
        facts = {"drive_healths": ["FAILED", "FAILED", "OK"]}
        assert evaluate_condition(cond, facts, REGISTRY) is True

    def test_count_without_equals_counts_list_length(self) -> None:
        cond = Condition(metric="drive_healths", operator="COUNT_GT", value=1)
        assert evaluate_condition(cond, {"drive_healths": ["a", "b"]}, REGISTRY) is True


class TestComposite:
    def test_all_of_requires_every_child_true(self) -> None:
        cond = Condition(
            all_of=[
                Condition(metric="down", operator="GTE", value=1),
                Condition(metric="up", operator="GTE", value=1),
            ]
        )
        assert evaluate_condition(cond, {"down": 1, "up": 0}, REGISTRY) is False
        assert evaluate_condition(cond, {"down": 1, "up": 1}, REGISTRY) is True

    def test_any_of_requires_one_child_true(self) -> None:
        cond = Condition(
            any_of=[
                Condition(metric="down", operator="GTE", value=5),
                Condition(metric="up", operator="GTE", value=1),
            ]
        )
        assert evaluate_condition(cond, {"down": 0, "up": 1}, REGISTRY) is True

    def test_not_negates_child(self) -> None:
        cond = Condition(not_=Condition(metric="down", operator="GTE", value=1))
        assert evaluate_condition(cond, {"down": 0}, REGISTRY) is True
        assert evaluate_condition(cond, {"down": 1}, REGISTRY) is False


class TestModelValidation:
    def test_exactly_one_kind_required(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Condition()

    def test_leaf_requires_operator(self) -> None:
        with pytest.raises(ValueError, match="requires an operator"):
            Condition(metric="down")

    def test_empty_all_of_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Condition(all_of=[])


class TestWriteTimeValidation:
    def test_unknown_metric_rejected(self) -> None:
        cond = Condition(metric="nonexistent", operator="EQ", value=1)
        with pytest.raises(ConditionValidationError, match="unknown metric"):
            validate_condition(cond, REGISTRY)

    def test_operator_type_mismatch_rejected(self) -> None:
        cond = Condition(metric="drive_healths", operator="GT", value=1)
        with pytest.raises(ConditionValidationError, match="not valid for metric type"):
            validate_condition(cond, REGISTRY)

    def test_enum_value_outside_declared_set_rejected(self) -> None:
        cond = Condition(metric="power_state", operator="EQ", value="UNPLUGGED")
        with pytest.raises(ConditionValidationError, match="not a valid value"):
            validate_condition(cond, REGISTRY)

    def test_valid_condition_passes(self) -> None:
        cond = Condition(metric="down", operator="GTE", value=2)
        validate_condition(cond, REGISTRY)

    def test_depth_limit_enforced(self) -> None:
        cond = Condition(metric="down", operator="GTE", value=1)
        for _ in range(10):
            cond = Condition(all_of=[cond])
        with pytest.raises(ConditionValidationError, match="depth"):
            validate_condition(cond, REGISTRY)


def test_leaf_metrics_collects_all_referenced_metrics() -> None:
    cond = Condition(
        all_of=[
            Condition(metric="down", operator="GTE", value=1),
            Condition(not_=Condition(metric="up", operator="EQ", value=0)),
        ]
    )
    assert set(leaf_metrics(cond)) == {"down", "up"}
