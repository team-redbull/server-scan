"""
The seeded checks, and which servers they can actually fire for: every
condition names a registered metric, and a check that only fires for one
vendor's vocabulary is not fleet coverage.
"""

from __future__ import annotations

from typing import Any, cast

from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.pipeline import health_from_state
from app.domain.enums import HealthSeverity, LinkState, Vendor
from app.domain.models.connectivity import Connectivity, ConnectivityFacts
from app.domain.models.hardware import Gpu, Hardware, Power, Psu, Storage, StorageDrive
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Identity, Server
from app.domain.services.health.evaluate import CATEGORIES, evaluate_health
from app.domain.services.health.facts import extract_facts
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.domain.services.health.metrics import build_default_registry
from app.utils.timeutil import utcnow


def _metrics_in(condition: object) -> list[str]:
    """
    Every metric name a condition tree references.

    Args:
        condition (object): A `Condition`, or None.

    Returns:
        list[str]: Metric names, including those inside all_of/any_of/not.
    """
    if condition is None:
        return []
    names: list[str] = []
    metric = getattr(condition, "metric", None)
    if metric:
        names.append(str(metric))
    for key in ("all_of", "any_of"):
        for child in getattr(condition, key, None) or []:
            names.extend(_metrics_in(child))
    names.extend(_metrics_in(getattr(condition, "not_", None)))
    return names


def _server(**hardware: object) -> Server:
    """
    A server carrying only the hardware a test cares about.

    Args:
        **hardware (object): Fields to set on `Hardware`/`NetworkInfo`.

    Returns:
        Server: A minimally-populated server.
    """
    network = hardware.pop("network", NetworkInfo())
    return Server.model_construct(
        # `model_construct` skips validation, so a required field left out is
        # simply absent and reading it raises. `name` is read by the 5TB/10TB
        # facts; blank means neither rule fires, which is what these tests want.
        name="",
        hardware=Hardware(**hardware),  # ty: ignore[invalid-argument-type]
        network=network,
    )


class TestSeededPolicies:
    def test_every_condition_references_a_registered_metric(self) -> None:
        """A policy naming an unknown metric is rejected on save, so a
        seeded one that did would break startup seeding rather than fail
        quietly.
        """
        registry = build_default_registry()
        for policy in default_system_policies():
            for metric in _metrics_in(policy.condition):
                assert metric in registry, f"{policy.policy_key} references unknown {metric!r}"

    def test_every_evidence_field_references_a_registered_metric(self) -> None:
        """Evidence is interpolated into the message; an unknown metric
        would render an empty finding message.
        """
        registry = build_default_registry()
        for policy in default_system_policies():
            for field in policy.evidence:
                assert field.metric in registry

    def test_policy_keys_are_unique(self) -> None:
        """Same `policy_key` means "these compete for one winner" (ADR-0005),
        so two defaults sharing one would silently shadow each other.
        """
        keys = [p.policy_key for p in default_system_policies()]
        assert len(keys) == len(set(keys))


class TestPsuFailureTiers:
    """One PSU down is a redundant pair doing its job (MAJOR); the server's
    only PSU, or two or more, down is a real loss (CRITICAL) — 2026-09-17,
    mirroring the existing OS-disk MAJOR/CRITICAL tier.
    """

    @staticmethod
    def _power_severity(psus: list[Psu]) -> HealthSeverity:
        server = _server(power=Power(psus=psus))
        state = evaluate_health(
            extract_facts(server),
            default_system_policies(),
            build_default_registry(),
            vendor="dell",
            manager_type=None,
            site_id=None,
        )
        return state.categories["power"].severity

    def test_one_of_two_down_is_major(self) -> None:
        psus = [Psu(id="0", health="UP"), Psu(id="1", health="DOWN")]
        assert self._power_severity(psus) == HealthSeverity.MAJOR

    def test_two_of_two_down_is_critical(self) -> None:
        psus = [Psu(id="0", health="DOWN"), Psu(id="1", health="DOWN")]
        assert self._power_severity(psus) == HealthSeverity.CRITICAL

    def test_the_only_psu_down_is_critical_not_major(self) -> None:
        """A single-PSU server has no redundancy to lose — its only supply
        failing is a real outage, whatever the redundant-pair case is.
        """
        assert self._power_severity([Psu(id="0", health="DOWN")]) == HealthSeverity.CRITICAL

    def test_all_psus_healthy_is_not_flagged(self) -> None:
        psus = [Psu(id="0", health="UP"), Psu(id="1", health="UP")]
        assert self._power_severity(psus) != HealthSeverity.MAJOR
        assert self._power_severity(psus) != HealthSeverity.CRITICAL


class TestCoverageAcrossVendors:
    """A check that only fires for one vendor is not fleet coverage."""

    def test_a_failed_psu_is_seen_whichever_vocabulary_reported_it(self) -> None:
        """Every provider normalizes a PSU onto UP/DOWN, so one fact
        covers UCS, Intersight and Redfish alike.
        """
        server = _server(power=Power(psus=[Psu(id="0", health="UP"), Psu(id="1", health="DOWN")]))
        assert extract_facts(server)["power.failed_psu_count"] == 1

    def test_a_failed_gpu_is_seen_in_both_vocabularies(self) -> None:
        """Redfish maps GPU health onto HealthSeverity (CRITICAL) while
        UCS and Intersight map OperState (DOWN). Counting only one spelling
        would cover one vendor and silently miss the other.
        """
        redfish_style = _server(gpus=[Gpu(health=HealthSeverity.CRITICAL.value)])
        cisco_style = _server(gpus=[Gpu(health="DOWN")])
        assert extract_facts(redfish_style)["gpu.failed_count"] == 1
        assert extract_facts(cisco_style)["gpu.failed_count"] == 1

    def test_a_healthy_gpu_counts_in_neither(self) -> None:
        for healthy in ("UP", HealthSeverity.HEALTHY.value):
            assert extract_facts(_server(gpus=[Gpu(health=healthy)]))["gpu.failed_count"] == 0

    def test_a_degraded_drive_is_counted_apart_from_a_failed_one(self) -> None:
        """The WARNING check must not swallow the CRITICAL one, or a dead
        drive would downgrade to a warning.
        """
        server = _server(
            storage=Storage(
                drives=[
                    StorageDrive(id="0", health=HealthSeverity.WARNING.value),
                    StorageDrive(id="1", health=HealthSeverity.CRITICAL.value),
                ]
            )
        )
        facts = extract_facts(server)
        assert facts["storage.warning_drive_count"] == 1
        assert facts["storage.failed_drive_count"] == 1


class TestNoFalseAlarms:
    def test_unused_nics_do_not_trip_the_link_check(self) -> None:
        """The fact counts links UP, not links down, so idle ports do not
        read as failures. The verdict is separate: one link up is CRITICAL.
        """
        server = _server(
            network=NetworkInfo(
                interfaces=[
                    NetworkInterface(name="a", link_state=LinkState.UP),
                    NetworkInterface(name="b", link_state=LinkState.DOWN),
                    NetworkInterface(name="c", link_state=LinkState.DOWN),
                ]
            )
        )
        facts = extract_facts(server)
        assert facts["network.links_up_count"] == 1
        assert facts["network.interface_count"] == 3

    def test_a_server_with_no_interfaces_read_is_not_all_links_down(self) -> None:
        """`interface_count GTE 1` is what separates "every link is down"
        from "nothing was reported" — a collection gap must not alert.
        """
        facts = extract_facts(_server())
        assert facts["network.interface_count"] == 0
        assert facts["network.links_up_count"] == 0

    def test_correctable_gpu_errors_are_not_counted(self) -> None:
        """A correctable ECC error is the mechanism working as designed.
        Counting it would fire on healthy accelerators constantly.
        """
        server = _server(gpus=[Gpu(correctable_error_count=4096, uncorrectable_error_count=0)])
        assert extract_facts(server)["gpu.uncorrectable_error_count"] == 0

    def test_uncorrectable_errors_sum_across_cards(self) -> None:
        """One policy on the server total is what gets alerted on; the
        per-card detail is on the document for whoever investigates.
        """
        server = _server(
            gpus=[Gpu(uncorrectable_error_count=1), Gpu(uncorrectable_error_count=2), Gpu()]
        )
        assert extract_facts(server)["gpu.uncorrectable_error_count"] == 3

    def test_a_server_with_no_gpus_reports_zero_not_a_failure(self) -> None:
        """Most of the fleet has no GPU at all; the GPU checks must be
        silent there rather than firing on absence.
        """
        facts = extract_facts(_server())
        assert facts["gpu.count"] == 0
        assert facts["gpu.failed_count"] == 0


class TestUnknownIsNotAVerdict:
    """A field a collector could not read must not be judged (ADR-0027)."""

    @staticmethod
    def _network_severity(*states: LinkState) -> HealthSeverity:
        """
        Run the seeded defaults over a server with just these link states.

        Args:
            states (LinkState): One per interface, in order.

        Returns:
            HealthSeverity: The rolled-up `network` category severity.
        """
        server = _server(
            network=NetworkInfo(
                interfaces=[
                    NetworkInterface(name=f"nic{n}", link_state=state)
                    for n, state in enumerate(states, start=1)
                ]
            )
        )
        state = evaluate_health(
            extract_facts(server),
            default_system_policies(),
            build_default_registry(),
            vendor="cisco",
            manager_type=None,
            site_id=None,
        )
        return state.categories["network"].severity

    def test_all_link_states_unknown_is_not_critical(self) -> None:
        """The symptom this rule exists for: UCS reports UNKNOWN on ~99.75%
        of vNICs, which used to read as "no link is up" and marked nearly
        every Cisco server CRITICAL.
        """
        assert self._network_severity(LinkState.UNKNOWN, LinkState.UNKNOWN) == (
            HealthSeverity.UNKNOWN
        )

    def test_one_up_link_beside_unknowns_is_not_major(self) -> None:
        """Redundancy cannot be judged against links nothing could read."""
        assert self._network_severity(LinkState.UP, LinkState.UNKNOWN) != HealthSeverity.MAJOR

    def test_links_that_really_are_all_down_are_still_critical(self) -> None:
        assert self._network_severity(LinkState.DOWN, LinkState.DOWN) == HealthSeverity.CRITICAL

    def test_one_up_of_two_real_readings_is_critical(self) -> None:
        """Treated the same as every link down (docs/adr/0027, 2026-09-23 update)."""
        assert self._network_severity(LinkState.UP, LinkState.DOWN) == HealthSeverity.CRITICAL

    def test_no_other_fact_counts_unknown_as_a_failure(self) -> None:
        """The audit behind ADR-0027: every other health fact already
        counts only definite-bad readings, so UNKNOWN is excluded there.
        """
        facts = extract_facts(
            _server(
                storage=Storage(drives=[StorageDrive(id="d", health=HealthSeverity.UNKNOWN.value)]),
                power=Power(psus=[Psu(id="p", health="UNKNOWN")]),
                gpus=[Gpu(health="UNKNOWN")],
            )
        )
        assert facts["storage.failed_drive_count"] == 0
        assert facts["storage.warning_drive_count"] == 0
        assert facts["storage.data_bad_disk_count"] == 0
        assert facts["power.failed_psu_count"] == 0
        assert facts["gpu.failed_count"] == 0


class TestManagerScopedDefaults:
    """The fabric policies are scoped to the two fabric-interconnect collectors (ADR-0030)."""

    @staticmethod
    def _connectivity_severity(source_provider: str) -> HealthSeverity | None:
        """
        Evaluate the seeded defaults for a Cisco server with two fabric paths down.

        Args:
            source_provider (str): The collector that wrote the server.

        Returns:
            HealthSeverity | None: The `connectivity` category severity, or
                None when no connectivity policy fired.
        """
        now = utcnow()
        server = Server(
            id="srv_fabric",
            name="ocp4-prod-tlv-compute-01",
            identity=Identity(vendor=Vendor.CISCO),
            source_provider=source_provider,
            connectivity=Connectivity(
                facts=ConnectivityFacts(fabric_paths_total=2, fabric_paths_down=2)
            ),
            created_at=now,
            updated_at=now,
        )
        service = HealthPolicyService(
            policy_repo=cast(Any, None), registry=build_default_registry()
        )
        state = service.evaluate_with_policies(server, default_system_policies())
        category = state.categories.get("connectivity")
        return category.severity if category is not None else None

    def test_fires_for_a_ucs_central_server(self) -> None:
        assert self._connectivity_severity("UCS_CENTRAL") == HealthSeverity.CRITICAL

    def test_fires_for_an_intersight_server(self) -> None:
        assert self._connectivity_severity("INTERSIGHT") == HealthSeverity.CRITICAL

    def test_does_not_fire_for_a_oneview_server(self) -> None:
        assert self._connectivity_severity("ONEVIEW") != HealthSeverity.CRITICAL

    def test_every_other_default_is_unscoped(self) -> None:
        scoped = {p.name for p in default_system_policies() if p.scope.specificity() > 0}
        assert scoped == {"UCS fabric path down (warning)", "UCS fabric paths down (critical)"}


class TestEveryPolicyCategoryReachesOverall:
    """A category a default policy names must be one the rollup iterates."""

    def test_every_default_category_is_rolled_up(self) -> None:
        assert {p.category for p in default_system_policies()} <= set(CATEGORIES)

    def test_a_failed_gpu_makes_the_server_critical(self) -> None:
        """Regression: `gpu` was missing from CATEGORIES, so "GPU failed" fired
        and the server still read HEALTHY overall.
        """
        now = utcnow()
        server = Server(
            id="srv_gpu",
            name="ocp4-prod-tlv-gpu-01",
            identity=Identity(vendor=Vendor.DELL),
            source_provider="OPENMANAGE",
            hardware=Hardware(gpus=[Gpu(health="DOWN")]),
            created_at=now,
            updated_at=now,
        )
        service = HealthPolicyService(
            policy_repo=cast(Any, None), registry=build_default_registry()
        )
        state = service.evaluate_with_policies(server, default_system_policies())
        assert state.categories["gpu"].severity == HealthSeverity.CRITICAL
        assert state.overall == HealthSeverity.CRITICAL
        assert health_from_state(state).gpu == HealthSeverity.CRITICAL


class TestTwoUplinksAreRequired:
    """Two bonded uplinks are a platform requirement, so one is not a smaller
    working server — it is a server that cannot join the fabric."""

    @staticmethod
    def _network_severity(*states: LinkState) -> HealthSeverity:
        """
        Run the seeded defaults over a server with just these link states.

        Args:
            states (LinkState): One per interface, in order.

        Returns:
            HealthSeverity: The rolled-up `network` category severity.
        """
        return TestUnknownIsNotAVerdict._network_severity(*states)

    def test_two_up_links_are_healthy(self) -> None:
        assert self._network_severity(LinkState.UP, LinkState.UP) == HealthSeverity.HEALTHY

    def test_a_server_with_only_one_port_is_critical(self) -> None:
        """The gap this closes. The old condition required two READABLE links
        before it would judge anything, so a machine presenting a single port
        was the one shape that passed by having too few NICs to fail.
        """
        assert self._network_severity(LinkState.UP) == HealthSeverity.CRITICAL

    def test_one_up_beside_an_unreadable_port_is_critical(self) -> None:
        """Two uplinks must be OBSERVED, not assumed. The second port may well
        be up, but nothing read it, so the server is not certified — the
        install path needs two MACs it can actually bond.
        """
        assert self._network_severity(LinkState.UP, LinkState.UNKNOWN) == HealthSeverity.CRITICAL

    def test_a_third_spare_port_does_not_change_a_good_pair(self) -> None:
        """Two up is the requirement, not the maximum."""
        assert (
            self._network_severity(LinkState.UP, LinkState.UP, LinkState.DOWN)
            == HealthSeverity.HEALTHY
        )
