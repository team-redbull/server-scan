"""Reading a cluster's own report of a host.

The hostname rules are the whole reason this design works across vendors,
and they are pure functions over the JSON a cluster returns — so they are
tested here rather than against a cluster.

The case that matters: Cisco reports the server's real name as its
hostname, Dell reports one derived from a MAC. On Dell the real name is
only in the **requested** hostname an operator set, so the requested one
has to win, and the reported one has to remain the fallback for everything
else.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import OpenShiftState
from app.infrastructure.openshift.records import (
    agent_observation,
    clean_hostname,
    name_excluded,
    node_internal_ip,
    node_observation,
)


def _agent(
    *,
    requested: str | None = None,
    reported: str | None = None,
    cluster: str | None = None,
    name: str = "agent-abc123",
    serial: str | None = None,
) -> dict[str, Any]:
    """
    One `Agent` custom resource.

    Args:
        requested (str | None): `spec.hostname`, the operator's override.
        reported (str | None): `status.inventory.hostname`, self-reported.
        cluster (str | None): The bound cluster, if any.
        name (str): `metadata.name`.
        serial (str | None): `status.inventory.systemVendor.serialNumber`
            (ADR-0036).

    Returns:
        dict[str, Any]: The resource as the API would return it.
    """
    spec: dict[str, Any] = {}
    if requested is not None:
        spec["hostname"] = requested
    if cluster is not None:
        spec["clusterDeploymentName"] = {"name": cluster}
    inventory: dict[str, Any] = {}
    if reported is not None:
        inventory["hostname"] = reported
    if serial is not None:
        inventory["systemVendor"] = {"serialNumber": serial}
    return {
        "metadata": {"name": name},
        "spec": spec,
        "status": {"inventory": inventory},
    }


class TestCleanHostname:
    """What counts as a usable hostname."""

    def test_a_domain_is_stripped(self) -> None:
        """Node names often carry one and server names never do."""
        assert clean_hostname("ocp4-tlv-worker-01.example.com") == "ocp4-tlv-worker-01"

    def test_case_and_whitespace_are_normalized(self) -> None:
        """`Server.name_normalized` is lowercase, so the key must be too."""
        assert clean_hostname("  OCP4-TLV-Worker-01  ") == "ocp4-tlv-worker-01"

    def test_empty_becomes_none_not_empty_string(self) -> None:
        """Load-bearing, not tidiness: `agent_observation` chains the two
        hostname sources with `or` (ADR-0024).
        """
        assert clean_hostname("") is None
        assert clean_hostname("   ") is None
        assert clean_hostname(None) is None


class TestAgentHostname:
    """Requested first, reported second."""

    def test_the_requested_hostname_wins(self) -> None:
        """The Dell case: the reported hostname is MAC-derived and matches
        nothing, while the requested one is the server's real name.
        """
        observation = agent_observation(
            _agent(requested="ocp4-tlv-worker-01", reported="b0-7b-25-1a-44-c0"),
            mce_name="mce-tlv",
        )

        assert observation is not None
        assert observation.hostname == "ocp4-tlv-worker-01"

    def test_the_reported_hostname_is_the_fallback(self) -> None:
        """The Cisco case: nobody set a requested hostname because the
        machine already reports its real name.
        """
        observation = agent_observation(_agent(reported="ocp4-tlv-worker-02"), mce_name="mce-tlv")

        assert observation is not None
        assert observation.hostname == "ocp4-tlv-worker-02"

    def test_a_blank_requested_hostname_falls_through(self) -> None:
        """The field exists but was never filled in. Short-circuiting on
        its presence rather than its content would strand every Dell host
        on a MAC-derived name.
        """
        observation = agent_observation(
            _agent(requested="   ", reported="ocp4-tlv-worker-03"), mce_name="mce-tlv"
        )

        assert observation is not None
        assert observation.hostname == "ocp4-tlv-worker-03"

    def test_neither_hostname_is_skipped_not_guessed(self) -> None:
        """An Agent nothing can be correlated on is reported as unmatched,
        never attached to a plausible-looking server.
        """
        assert agent_observation(_agent(), mce_name="mce-tlv") is None


class TestAgentState:
    """Bound to a cluster, or sitting in the hub's inventory."""

    def test_a_bound_agent_is_installed_and_names_its_cluster(self) -> None:
        observation = agent_observation(
            _agent(requested="ocp4-tlv-worker-01", cluster="hc-tlv-02"), mce_name="mce-tlv"
        )

        assert observation is not None
        assert observation.lifecycle_state is OpenShiftState.INSTALLED
        assert observation.cluster_name == "hc-tlv-02"
        assert observation.mce_name == "mce-tlv"

    def test_an_unbound_agent_is_in_inventory_with_no_cluster(self) -> None:
        """`cluster_name` stays None rather than empty: there is no
        cluster, which is a different claim from a name that went unread.
        """
        observation = agent_observation(_agent(requested="ocp4-tlv-spare-07"), mce_name="mce-tlv")

        assert observation is not None
        assert observation.lifecycle_state is OpenShiftState.INSTALLED_TO_INVENTORY
        assert observation.cluster_name is None
        assert observation.mce_name == "mce-tlv"


class TestNameExcluded:
    """The one filter for both `nodes` and `agents` (ADR-0024, 2026-09-22)."""

    def test_a_matching_substring_excludes(self) -> None:
        assert name_excluded("ocp4-tlv-master-0", ("infra", "control-plane", "master"))

    def test_no_matching_substring_keeps_it(self) -> None:
        assert not name_excluded("ocp4-tlv-compute-01", ("infra", "control-plane", "master"))

    def test_vcompute_does_not_also_exclude_compute(self) -> None:
        """A term matches only when it is itself a substring of the name —
        `compute` being a substring of `vcompute` does not run backwards.
        """
        assert name_excluded("ocp4-tlv-vcompute-01", ("vcompute",))
        assert not name_excluded("ocp4-tlv-compute-01", ("vcompute",))


class TestNodes:
    """A cluster's own node list."""

    def test_a_node_is_installed_in_the_cluster_reading_it(self) -> None:
        observation = node_observation(
            {"metadata": {"name": "ocp4-tlv-worker-01", "labels": {}}},
            cluster_name="ocp4-tlv",
        )

        assert observation is not None
        assert observation.lifecycle_state is OpenShiftState.INSTALLED
        assert observation.cluster_name == "ocp4-tlv"
        assert observation.mce_name is None

    def test_a_nameless_node_is_skipped(self) -> None:
        assert node_observation({"metadata": {}}, cluster_name="ocp4-tlv") is None

    def test_the_internal_ip_is_carried_for_the_serial_fallback(self) -> None:
        """ADR-0036: the SSH fallback connects to this address."""
        observation = node_observation(
            {
                "metadata": {"name": "ocp4-tlv-worker-01"},
                "status": {
                    "addresses": [
                        {"type": "Hostname", "address": "ocp4-tlv-worker-01.example.com"},
                        {"type": "InternalIP", "address": "10.0.0.5"},
                    ]
                },
            },
            cluster_name="ocp4-tlv",
        )

        assert observation is not None
        assert observation.address == "10.0.0.5"

    def test_a_node_with_no_addresses_has_none(self) -> None:
        observation = node_observation(
            {"metadata": {"name": "ocp4-tlv-worker-01"}}, cluster_name="ocp4-tlv"
        )

        assert observation is not None
        assert observation.address is None


class TestNodeInternalIp:
    """Extracting `InternalIP` from `Node.status.addresses` (ADR-0036)."""

    def test_internal_ip_among_several_address_types(self) -> None:
        node = {
            "status": {
                "addresses": [
                    {"type": "Hostname", "address": "ocp4-tlv-worker-01"},
                    {"type": "InternalIP", "address": "10.0.0.5"},
                    {"type": "ExternalIP", "address": "203.0.113.5"},
                ]
            }
        }
        assert node_internal_ip(node) == "10.0.0.5"

    def test_no_addresses_is_none(self) -> None:
        assert node_internal_ip({}) is None
        assert node_internal_ip({"status": {}}) is None
        assert node_internal_ip({"status": {"addresses": []}}) is None

    def test_no_internal_ip_among_other_types_is_none(self) -> None:
        node = {"status": {"addresses": [{"type": "Hostname", "address": "worker-01"}]}}
        assert node_internal_ip(node) is None


class TestAgentSerial:
    """`status.inventory.systemVendor.serialNumber`, no SSH needed (ADR-0036)."""

    def test_the_serial_is_read_when_present(self) -> None:
        observation = agent_observation(
            _agent(requested="ocp4-tlv-worker-01", serial="SN123"), mce_name="mce-tlv"
        )

        assert observation is not None
        assert observation.serial == "SN123"

    def test_no_serial_is_none_not_empty(self) -> None:
        observation = agent_observation(_agent(requested="ocp4-tlv-worker-01"), mce_name="mce-tlv")

        assert observation is not None
        assert observation.serial is None
