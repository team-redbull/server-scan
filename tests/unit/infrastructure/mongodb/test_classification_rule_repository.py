"""
Unit test (no I/O) for `default_system_rules`, resolved through the real
`classify()` engine against the estate's real hostnames: since 2026-09-08
every default is a broad, overlapping catch-all, so only priority/order
resolution — not raw pattern matching — says which one wins.
"""

from __future__ import annotations

import re

import pytest

from app.domain.enums import InstallationType, Vendor
from app.domain.models.classification_rule import PRIORITY_BANDS
from app.domain.services.classification import ClassifiableServer, classify
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb.classification_rule_repository import (
    _UPI_PATTERN,
    default_system_rules,
)

# The shipped default catalog. The seeded rules interpolate the
# configured site codes now, so these fixtures pin which set they
# were written against.
SITES = site_catalog("")

ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)


def _classify(name: str) -> InstallationType:
    """The installation type the real resolution engine gives `name`
    against the current shipped default rules.
    """
    server = ClassifiableServer(name=name, vendor=Vendor.DELL, manager_type=None, site_id=None)
    return classify(server, default_system_rules(SITES), ENGINE).installation_type


def test_rules_have_unique_ids_and_names() -> None:
    rules = default_system_rules(SITES)
    assert len({r.id for r in rules}) == len(rules)
    assert len({r.name for r in rules}) == len(rules)


def test_every_default_rule_is_a_locked_unscoped_system_rule() -> None:
    for rule in default_system_rules(SITES):
        assert rule.source == "SYSTEM_DEFAULT"
        assert rule.system is True
        assert rule.enabled is True
        assert rule.field == "name"
        low, high = PRIORITY_BANDS["SYSTEM_DEFAULT"]
        assert low <= rule.priority <= high
        # Unscoped: these encode a fleet-wide naming convention, not a
        # per-vendor or per-site preference.
        assert rule.scope.vendor is None
        assert rule.scope.manager_type is None
        assert rule.scope.site_id is None


@pytest.mark.parametrize(
    "name",
    [
        "ocp4-hypershift-five-01",
        "ocp4-hypershift-data-five-02",
        "ocp4-hypershift-bat-yam-99",
        "ocp-dell-r660-five-128c-1024gb-FCH1234567",
        "ocp-cisco-m6-nyc-64c-512gb-CIS0000124",
        # Broadened 2026-09-08: both hosted-cluster rules are now bare
        # prefix matches, so neither the site token nor the rest of the
        # old structured shape is required any more.
        "ocp4-hypershift",
        "ocp4-hypershift-anything-at-all",
        "ocp-anything-goes-here",
    ],
)
def test_hosted_cluster_hostnames_classify_as_hosted_cluster(name: str) -> None:
    """The hypershift names also match UPI's catch-all; HOSTED_CLUSTER's
    rules sort ahead of UPI's, so they win regardless.
    """
    assert _classify(name) == InstallationType.HOSTED_CLUSTER


@pytest.mark.parametrize(
    "name",
    [
        "ocp4-mce-five-01",
        "ocp4-mce-nyc-07",
        "ocp4-prod-mce-tlv-03",
        "OCP4-MCE-FIVE-01",  # ignore_case is the shared default for every rule
    ],
)
def test_mce_hostnames_classify_as_mce(name: str) -> None:
    """Every one of these also textually matches UPI's `ocp4` catch-all —
    MCE's rule sorts ahead of it (order=2 vs. UPI's order=3), so an MCE
    hub's own nodes are pulled out of the generic UPI bucket.
    """
    assert _classify(name) == InstallationType.MCE


@pytest.mark.parametrize(
    "name",
    [
        "ocp4-five-compute-01",
        "ocp4-nyc-control-plane-02",
        "ocp4-prod-tlv-infra-01",
        "ocp4-prep-five-compute-01",
        "ocp4-bat-yam-infra-07",
        # Since 2026-09-08 UPI is a catch-all: an invalid or missing site
        # token no longer means UNCLASSIFIED.
        "ocp4-tlvx-01",  # "tlv" is a substring, not a real site token
        "ocp4-prod-infra-01",  # no site token
        # Since 2026-09-10 UPI is unconditional (`.*`), so these are UPI too —
        # see `test_nothing_classifies_as_unclassified_any_more`.
        "random-server-0009",
        "some-unmanaged-box",
        "",
    ],
)
def test_upi_hostnames_classify_as_upi(name: str) -> None:
    assert _classify(name) == InstallationType.UPI


def test_nothing_classifies_as_unclassified_any_more() -> None:
    """UPI's pattern matches every string, `""` included, so under the
    system defaults `InstallationType.UNCLASSIFIED` is now unreachable.
    """
    for name in ("random-server-0009", "some-unmanaged-box", ""):
        assert _classify(name) != InstallationType.UNCLASSIFIED


def test_hosted_cluster_and_mce_outrank_the_upi_catch_all() -> None:
    """UPI matches every hostname (deliberate, 2026-09-10); `order` keeps the
    specific rules winning — HOSTED_CLUSTER (0-1) and MCE (2) sort ahead of
    UPI (3), so first-match-wins never reaches UPI for these names.
    """
    names_and_expected = [
        ("ocp4-hypershift-five-01", InstallationType.HOSTED_CLUSTER),
        ("ocp4-hypershift-data-five-02", InstallationType.HOSTED_CLUSTER),
        ("ocp4-mce-five-01", InstallationType.MCE),
    ]
    for name, expected in names_and_expected:
        assert re.search(_UPI_PATTERN, name), f"{name} was expected to also match UPI's pattern"
        assert _classify(name) == expected
