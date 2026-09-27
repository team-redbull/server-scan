"""Decoding a membership written before `OpenShiftState` was narrowed."""

from __future__ import annotations

import pytest

from app.domain.enums import OpenShiftState
from app.domain.models.openshift import OpenShiftLifecycle


@pytest.mark.parametrize("retired", ["UPI_NODE", "HOSTED_NODE", "UNKNOWN", "nonsense"])
def test_a_retired_state_decodes_as_available(retired: str) -> None:
    """Without this, every read path breaks against a pre-ADR-0024 database.

    Pydantic rejects an unknown enum member outright; ADR-0024 has what
    that cost when it shipped unhandled.
    """
    state = OpenShiftLifecycle.model_validate({"lifecycle_state": retired})

    assert state.lifecycle_state is OpenShiftState.AVAILABLE


@pytest.mark.parametrize("current", list(OpenShiftState))
def test_a_current_state_is_untouched(current: OpenShiftState) -> None:
    state = OpenShiftLifecycle.model_validate({"lifecycle_state": current.value})

    assert state.lifecycle_state is current


def test_a_document_written_before_reported_name_existed_still_loads() -> None:
    """ADR-0036 added `reported_name` after this shape had already shipped;
    a pre-existing document simply has no such key at all.
    """
    state = OpenShiftLifecycle.model_validate(
        {"lifecycle_state": "INSTALLED", "cluster_name": "ocp4-tlv"}
    )

    assert state.reported_name is None
