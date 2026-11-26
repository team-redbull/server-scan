"""The fleet naming convention predicate (ADR-0071)."""

from __future__ import annotations

import pytest

from app.domain.value_objects.fleet_name import matches_fleet_convention


@pytest.mark.parametrize(
    "name",
    [
        "ocp-c200-mtl-tlv-4b184et",
        "ocp-c123-mtl-site1-abc1234",
        "ocp-c45.2d-mtl-site2-xyz987",
        "ocp-c2.1n-mtl-x-y",
        "ocp-c203d-mtl-tlv-abc",
    ],
)
def test_convention_names_match(name: str) -> None:
    """Plain, dotted conf ids and the `d`/`n` markers are all the convention."""
    assert matches_fleet_convention(name)


@pytest.mark.parametrize(
    "name",
    [
        "ocp-tomer-compute-o01",
        "OCP-C200-MTL-TLV-4B184ET",
        "ocp-c200x-mtl-tlv-4b184et",
        "ocp-c200-mtl-tlv",
        "ocp-c-mtl-tlv-4b184et",
        "ocp-c200-mtl-tlv-4b184et-extra",
        "ocp-c2.-mtl-tlv-abc",
        "xocp-c200-mtl-tlv-4b184et",
        "",
    ],
)
def test_other_names_do_not_match(name: str) -> None:
    """Anything else, uppercase included, is renamed."""
    assert not matches_fleet_convention(name)
