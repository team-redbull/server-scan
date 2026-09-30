"""Classification carries the rule's name; the change event is readable and has no rule id."""

from app.application.services.pipeline import classification_from_result
from app.domain.enums import InstallationType
from app.domain.models.classification import Classification, classification_changed_data
from app.domain.services.classification import ClassificationResult


def _result() -> ClassificationResult:
    return ClassificationResult(
        installation_type=InstallationType.HOSTED_CLUSTER,
        rule_id="r1",
        rule_name="Hypershift",
        rule_source="SYSTEM_DEFAULT",
        matched_field="name",
        matched_pattern="^ocp4-hypershift",
        matched_value_preview=None,
        priority=1,
        specificity=1,
        conflicts=[],
        errors=[],
    )


def test_pipeline_persists_the_rule_name() -> None:
    c = classification_from_result(_result(), previous_version=0)
    assert c.matched_rule_name == "Hypershift"
    assert c.matched_pattern == "^ocp4-hypershift"


def test_a_legacy_document_without_the_name_loads() -> None:
    assert Classification.model_validate({"matched_rule_id": "x"}).matched_rule_name is None


def test_event_data_is_readable_and_drops_the_rule_id() -> None:
    c = classification_from_result(_result(), previous_version=0)
    assert classification_changed_data(InstallationType.UNCLASSIFIED, c) == {
        "from": "UNCLASSIFIED",
        "to": "HOSTED_CLUSTER",
        "matched_rule": "Hypershift",
        "matched_field": "name",
        "matched_pattern": "^ocp4-hypershift",
    }
