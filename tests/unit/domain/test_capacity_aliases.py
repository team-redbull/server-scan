"""Capacity-token aliasing for `GET /servers/available`'s pattern mode.

Every case here is about the "exactly one configured token" boundary —
ADR-0032 is explicit that a pattern merely containing a token as a
substring must not expand.
"""

from __future__ import annotations

import pytest

from app.domain.value_objects.capacity_aliases import (
    CapacityAliasCatalog,
    CapacityAliasConfigurationError,
    capacity_alias_catalog,
)

pytestmark = pytest.mark.unit

_SPEC = "5tb:hypershift(?!-data),10tb:hypershift-data"


def test_an_exact_token_expands() -> None:
    """The whole feature: `pattern=5tb` also matches a bare `hypershift`."""
    catalog = CapacityAliasCatalog.from_spec(_SPEC)

    assert catalog.expansion_for("5tb") == "hypershift(?!-data)"
    assert catalog.expansion_for("10tb") == "hypershift-data"


def test_matching_is_case_insensitive_on_the_token() -> None:
    catalog = CapacityAliasCatalog.from_spec(_SPEC)

    assert catalog.expansion_for("5TB") == "hypershift(?!-data)"


def test_a_pattern_merely_containing_the_token_does_not_expand() -> None:
    """The r650 example from the spec: a compound pattern is used verbatim."""
    catalog = CapacityAliasCatalog.from_spec(_SPEC)

    assert catalog.expansion_for("ocp-dell-r650-nyc-64c-128gb-10tb") is None
    assert catalog.expansion_for("ocp-dell-r650-five-128c-1024gb-5tb-") is None


def test_an_unconfigured_token_expands_nothing() -> None:
    assert CapacityAliasCatalog.from_spec(_SPEC).expansion_for("20tb") is None


def test_an_empty_spec_uses_the_shipped_default() -> None:
    catalog = CapacityAliasCatalog.from_spec("")

    assert catalog.expansion_for("5tb") == "hypershift(?!-data)"
    assert catalog.expansion_for("10tb") == "hypershift-data"


@pytest.mark.parametrize("spec", ["5tb", ":alias", "5tb:", "5tb:,10tb:x"])
def test_a_malformed_entry_fails_loudly(spec: str) -> None:
    with pytest.raises(CapacityAliasConfigurationError):
        CapacityAliasCatalog.from_spec(spec)


def test_the_catalog_is_cached_per_spec() -> None:
    assert capacity_alias_catalog(_SPEC) is capacity_alias_catalog(_SPEC)
