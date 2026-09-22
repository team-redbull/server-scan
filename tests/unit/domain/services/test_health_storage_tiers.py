"""OS disks vs data disks, the 10TB rule, and the MAJOR tier.

The OS/data split is inferred, not reported: no collector says which disk
the OS is on, so the smallest capacity present stands in for it. That
inference is what these tests pin, along with the two places it must
refuse to guess.
"""

from __future__ import annotations

from app.domain.enums import HEALTH_SEVERITY_RANK, HealthSeverity, LinkState
from app.domain.models.hardware import Hardware, Memory, MemoryModule, Storage, StorageDrive
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Server
from app.domain.services.health.evaluate import evaluate_health
from app.domain.services.health.facts import extract_facts
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.domain.services.health.metrics import build_default_registry

_GB = 1_000_000_000
_TB = 1_000_000_000_000


def _drive(capacity: int, health: str | None = None) -> StorageDrive:
    """
    One drive of a given size.

    Args:
        capacity (int): Capacity in bytes.
        health (str | None): Reported health, if any.

    Returns:
        StorageDrive: The drive.
    """
    return StorageDrive(id=f"d{capacity}-{health}", capacity_bytes=capacity, health=health)


def _server(name: str = "ocp4-nyc-prod-worker-01", **kwargs: object) -> Server:
    """
    A server with the drives, name and interfaces a test needs.

    Args:
        name (str): The server's name — the 10TB rule reads it.
        **kwargs (object): `drives` and/or `interfaces`.

    Returns:
        Server: A minimally-populated server.
    """
    drives = kwargs.get("drives") or []
    interfaces = kwargs.get("interfaces") or []
    dimms = kwargs.get("dimms") or []
    total = sum(d.capacity_bytes or 0 for d in drives)  # ty: ignore[not-iterable]
    return Server.model_construct(
        name=name,
        hardware=Hardware(
            storage=Storage(drives=drives, total_bytes=total),  # ty: ignore[invalid-argument-type]
            memory=Memory(modules=dimms),  # ty: ignore[invalid-argument-type]
        ),
        network=NetworkInfo(interfaces=interfaces),  # ty: ignore[invalid-argument-type]
    )


class TestSeverityTiers:
    """MAJOR sits between WARNING and CRITICAL."""

    def test_major_ranks_above_warning_and_below_critical(self) -> None:
        """Every worst-of rollup sorts by this rank, so getting it wrong
        would silently reorder which finding a server is judged by.
        """
        assert (
            HEALTH_SEVERITY_RANK[HealthSeverity.WARNING]
            < HEALTH_SEVERITY_RANK[HealthSeverity.MAJOR]
            < HEALTH_SEVERITY_RANK[HealthSeverity.CRITICAL]
        )


class TestOsDiskIdentification:
    """The smallest drives are the OS disks."""

    def test_the_smallest_pair_is_the_os_mirror(self) -> None:
        """The usual build: two small boot SSDs beside large data drives."""
        facts = extract_facts(
            _server(
                drives=[
                    _drive(480 * _GB),
                    _drive(480 * _GB),
                    _drive(4 * _TB),
                    _drive(4 * _TB),
                ]
            )
        )
        assert facts["storage.os_disk_count"] == 2
        assert facts["storage.data_disk_count"] == 2

    def test_identical_drives_yield_no_os_disks(self) -> None:
        """A server whose drives are all one size gives no basis for the
        split. Calling all 24 NVMe drives "OS disks" would turn a single
        degraded data drive into a MAJOR finding on every storage node.
        """
        facts = extract_facts(_server(drives=[_drive(4 * _TB) for _ in range(24)]))
        assert facts["storage.os_disk_count"] == 0
        assert facts["storage.data_disk_count"] == 24

    def test_drives_without_capacity_are_not_guessed_at(self) -> None:
        """A provider that reported no capacity gives nothing to sort by."""
        facts = extract_facts(_server(drives=[_drive(0), _drive(0)]))
        assert facts["storage.os_disk_count"] == 0


class TestOsDiskHealth:
    """One bad OS disk is MAJOR, two is CRITICAL."""

    def test_one_bad_os_disk_is_counted_alone(self) -> None:
        """Feeds the EQ 1 condition behind the MAJOR policy."""
        facts = extract_facts(
            _server(
                drives=[
                    _drive(480 * _GB, HealthSeverity.WARNING.value),
                    _drive(480 * _GB),
                    _drive(4 * _TB),
                ]
            )
        )
        assert facts["storage.os_bad_disk_count"] == 1

    def test_degraded_and_failed_both_count_as_bad(self) -> None:
        """On a boot mirror the distinction does not change what an
        operator does — the mirror is unprotected either way.
        """
        facts = extract_facts(
            _server(
                drives=[
                    _drive(480 * _GB, HealthSeverity.WARNING.value),
                    _drive(480 * _GB, HealthSeverity.CRITICAL.value),
                    _drive(4 * _TB),
                ]
            )
        )
        assert facts["storage.os_bad_disk_count"] == 2

    def test_a_bad_data_disk_does_not_count_as_an_os_disk(self) -> None:
        """The whole point of the split."""
        facts = extract_facts(
            _server(
                drives=[
                    _drive(480 * _GB),
                    _drive(480 * _GB),
                    _drive(4 * _TB, HealthSeverity.CRITICAL.value),
                ]
            )
        )
        assert facts["storage.os_bad_disk_count"] == 0
        assert facts["storage.data_bad_disk_count"] == 1


class TestOsDiskSeverityTiers:
    """Two bad OS disks is MAJOR if both are only predictive failure
    (WARNING), CRITICAL only once one has actually failed.
    """

    @staticmethod
    def _storage_severity(drives: list[StorageDrive]) -> HealthSeverity:
        state = evaluate_health(
            extract_facts(_server(drives=drives)),
            default_system_policies(),
            build_default_registry(),
            vendor="dell",
            manager_type=None,
            site_id=None,
        )
        return state.categories["storage"].severity

    def test_two_warning_os_disks_is_major_not_critical(self) -> None:
        severity = self._storage_severity(
            [
                _drive(480 * _GB, HealthSeverity.WARNING.value),
                _drive(480 * _GB, HealthSeverity.WARNING.value),
                _drive(4 * _TB),
            ]
        )
        assert severity == HealthSeverity.MAJOR

    def test_two_bad_os_disks_with_one_actually_failed_is_critical(self) -> None:
        severity = self._storage_severity(
            [
                _drive(480 * _GB, HealthSeverity.WARNING.value),
                _drive(480 * _GB, HealthSeverity.CRITICAL.value),
                _drive(4 * _TB),
            ]
        )
        assert severity == HealthSeverity.CRITICAL

    def test_two_failed_os_disks_is_critical(self) -> None:
        severity = self._storage_severity(
            [
                _drive(480 * _GB, HealthSeverity.CRITICAL.value),
                _drive(480 * _GB, HealthSeverity.CRITICAL.value),
                _drive(4 * _TB),
            ]
        )
        assert severity == HealthSeverity.CRITICAL


class TestDataDiskSeverityTiersOnLargeStorageServer:
    """The same MAJOR/CRITICAL split, on the 10TB-node data-disk policy."""

    @staticmethod
    def _storage_severity(drives: list[StorageDrive]) -> HealthSeverity:
        state = evaluate_health(
            extract_facts(_server(name="ocp4-nyc-10tb-01", drives=drives)),
            default_system_policies(),
            build_default_registry(),
            vendor="dell",
            manager_type=None,
            site_id=None,
        )
        return state.categories["storage"].severity

    def test_two_warning_data_disks_is_major_not_critical(self) -> None:
        severity = self._storage_severity(
            [
                _drive(480 * _GB),
                _drive(480 * _GB),
                _drive(4 * _TB, HealthSeverity.WARNING.value),
                _drive(4 * _TB, HealthSeverity.WARNING.value),
            ]
        )
        assert severity == HealthSeverity.MAJOR

    def test_two_bad_data_disks_with_one_actually_failed_is_critical(self) -> None:
        severity = self._storage_severity(
            [
                _drive(480 * _GB),
                _drive(480 * _GB),
                _drive(4 * _TB, HealthSeverity.WARNING.value),
                _drive(4 * _TB, HealthSeverity.CRITICAL.value),
            ]
        )
        assert severity == HealthSeverity.CRITICAL


class TestLargeStorageName:
    """The 10TB build is recorded only in the server's name."""

    def test_the_token_is_matched_case_insensitively(self) -> None:
        """Names arrive in whatever case the vendor reports."""
        assert extract_facts(_server(name="ocp4-nyc-10tb-01"))["server.name_has_10tb"]
        assert extract_facts(_server(name="OCP4-NYC-10TB-01"))["server.name_has_10tb"]

    def test_an_ordinary_server_does_not_match(self) -> None:
        """Otherwise every server would take the large-storage thresholds."""
        facts = extract_facts(_server(name="ocp4-nyc-prod-worker-01"))
        assert facts["server.name_has_10tb"] is False

    def test_total_capacity_is_exposed_for_the_capacity_mismatch_check(self) -> None:
        facts = extract_facts(_server(name="ocp4-nyc-10tb-01", drives=[_drive(4 * _TB)]))
        assert facts["storage.total_bytes"] == 4 * _TB

    def test_the_two_build_tokens_are_independent(self) -> None:
        """A 5TB node must not take the 10TB thresholds, or an ordinary
        5 TB machine would read as a wildly undersized 10TB one.
        """
        facts = extract_facts(_server(name="ocp4-nyc-5tb-01"))
        assert facts["server.name_has_5tb"] is True
        assert facts["server.name_has_10tb"] is False

    def test_a_10tb_name_does_not_also_match_5tb(self) -> None:
        """ "10tb" contains no "5tb", but this pins it: if the tokens ever
        overlap, a 10TB node would take both rules and contradict itself.
        """
        facts = extract_facts(_server(name="ocp4-nyc-10tb-01"))
        assert facts["server.name_has_10tb"] is True
        assert facts["server.name_has_5tb"] is False

    def test_a_larger_number_containing_the_token_does_not_match(self) -> None:
        """A real 35TB-class server false-positived as "5TB" (2026-09-17):
        "5tb" is a substring of "35tb", but not one of its own segments.
        """
        facts = extract_facts(_server(name="ocp-dell-r660-five-192-1536gb-35tb-DEL0001234"))
        assert facts["server.name_has_5tb"] is False
        assert facts["server.name_has_10tb"] is False

    def test_a_larger_10tb_lookalike_does_not_match_either(self) -> None:
        assert extract_facts(_server(name="ocp4-nyc-110tb-01"))["server.name_has_10tb"] is False


class TestNameCapacityMismatch:
    """`storage.name_capacity_mismatch` — one symmetric rule for any
    `-<N>tb` name token, replacing the old 5TB/10TB-specific ones
    (2026-09-17).
    """

    def test_parses_any_capacity_token_not_just_5_and_10(self) -> None:
        facts = extract_facts(_server(name="ocp4-nyc-20tb-01"))
        assert facts["storage.name_capacity_bytes"] == 20 * _TB

    def test_no_token_means_no_capacity_fact_at_all(self) -> None:
        facts = extract_facts(_server(name="ocp4-nyc-prod-worker-01"))
        assert facts["storage.name_capacity_bytes"] is None
        assert facts["storage.capacity_deviation_bytes"] is None

    def test_deviation_is_the_absolute_difference(self) -> None:
        facts = extract_facts(_server(name="ocp4-nyc-10tb-01", drives=[_drive(8 * _TB)]))
        assert facts["storage.capacity_deviation_bytes"] == 2 * _TB

    @staticmethod
    def _storage_severity(name: str, total_bytes: int) -> HealthSeverity:
        server = _server(name=name, drives=[_drive(total_bytes)])
        state = evaluate_health(
            extract_facts(server),
            default_system_policies(),
            build_default_registry(),
            vendor="dell",
            manager_type=None,
            site_id=None,
        )
        return state.categories["storage"].severity

    def test_within_1_5tb_either_way_is_not_critical(self) -> None:
        assert self._storage_severity("ocp4-nyc-10tb-01", 9 * _TB) != HealthSeverity.CRITICAL
        assert self._storage_severity("ocp4-nyc-10tb-01", 11 * _TB) != HealthSeverity.CRITICAL

    def test_more_than_1_5tb_short_is_critical(self) -> None:
        # The old undersized-only rule's own case, now symmetric.
        assert self._storage_severity("ocp4-nyc-10tb-01", 7 * _TB) == HealthSeverity.CRITICAL

    def test_more_than_1_5tb_over_is_critical_too(self) -> None:
        # The old oversized-only rule's own case, on the 10TB side this time.
        assert self._storage_severity("ocp4-nyc-10tb-01", 12 * _TB) == HealthSeverity.CRITICAL

    def test_the_35tb_regression_case_is_now_correctly_evaluated(self) -> None:
        """The bug this whole rule was rewritten over: a real 35TB-class
        server actually reporting ~35TB must never fire — the old rule
        would have false-positived it as an oversized "5TB" box.
        """
        name = "ocp-dell-r660-five-192-1536gb-35tb-DEL0001234"
        assert self._storage_severity(name, 35 * _TB) != HealthSeverity.CRITICAL

    def test_a_server_with_no_capacity_token_is_never_judged_by_this_rule(self) -> None:
        assert self._storage_severity("ocp4-nyc-prod-worker-01", 1) != HealthSeverity.CRITICAL


class TestNothingReadIsUnknown:
    """A category nothing was read for is UNKNOWN and judges nothing
    (ADR-0027, 2026-09-21) — the `-10tb` server whose storage was never
    read used to be CRITICAL, and every unread category read HEALTHY.
    """

    @staticmethod
    def _state(server: Server):  # noqa: ANN205 - HealthState, kept local to the test
        return evaluate_health(
            extract_facts(server),
            default_system_policies(),
            build_default_registry(),
            vendor="dell",
            manager_type=None,
            site_id=None,
        )

    def test_a_10tb_server_with_no_storage_read_is_not_critical(self) -> None:
        state = self._state(_server(name="ocp-dell-r750-five-128c-1024gb-10tb-DEL0001891"))
        assert state.categories["storage"].severity == HealthSeverity.UNKNOWN
        assert not [e for e in state.evaluations if e.active]

    def test_drives_listed_without_capacities_do_not_trip_the_mismatch(self) -> None:
        state = self._state(
            _server(name="ocp4-nyc-10tb-01", drives=[StorageDrive(id="d0", capacity_bytes=None)])
        )
        assert state.categories["storage"].severity == HealthSeverity.HEALTHY

    def test_nothing_read_anywhere_is_unknown_overall(self) -> None:
        state = self._state(_server())
        assert state.overall == HealthSeverity.UNKNOWN
        assert {c.severity for c in state.categories.values()} == {HealthSeverity.UNKNOWN}

    def test_one_category_read_makes_overall_healthy_not_unknown(self) -> None:
        state = self._state(_server(drives=[_drive(1 * _TB)]))
        assert state.categories["storage"].severity == HealthSeverity.HEALTHY
        assert state.categories["network"].severity == HealthSeverity.UNKNOWN
        assert state.overall == HealthSeverity.HEALTHY

    def test_a_category_that_was_read_still_fires(self) -> None:
        state = self._state(
            _server(
                drives=[
                    _drive(480 * _GB, "CRITICAL"),
                    _drive(480 * _GB, "CRITICAL"),
                    _drive(4 * _TB),
                ]
            )
        )
        assert state.categories["storage"].severity == HealthSeverity.CRITICAL
        assert state.overall == HealthSeverity.CRITICAL


class TestDegradedDimms:
    """DIMM health, which nothing populated before."""

    def test_a_degraded_dimm_is_counted(self) -> None:
        """The check the WARNING policy reads."""
        facts = extract_facts(
            _server(
                dimms=[
                    MemoryModule(slot="A1", health=HealthSeverity.HEALTHY.value),
                    MemoryModule(slot="A2", health=HealthSeverity.WARNING.value),
                ]
            )
        )
        assert facts["memory.dimm_count"] == 2
        assert facts["memory.degraded_dimm_count"] == 1

    def test_a_failed_dimm_counts_too(self) -> None:
        """Both states mean the same thing to whoever schedules the swap."""
        facts = extract_facts(
            _server(dimms=[MemoryModule(slot="A1", health=HealthSeverity.CRITICAL.value)])
        )
        assert facts["memory.degraded_dimm_count"] == 1

    def test_a_provider_that_reports_no_dimms_never_fires(self) -> None:
        """UCS and Intersight do not read per-DIMM health, so their servers
        must stay silent rather than reporting zero DIMMs as a fault.
        """
        facts = extract_facts(_server())
        assert facts["memory.dimm_count"] == 0
        assert facts["memory.degraded_dimm_count"] == 0


class TestSingleLinkUp:
    """Exactly one link up is MAJOR, and must not collide with zero."""

    def test_one_of_two_up_is_the_major_case(self) -> None:
        """Redundancy gone, server still reachable."""
        facts = extract_facts(
            _server(
                interfaces=[
                    NetworkInterface(name="a", link_state=LinkState.UP),
                    NetworkInterface(name="b", link_state=LinkState.DOWN),
                ]
            )
        )
        assert facts["network.links_up_count"] == 1
        assert facts["network.interface_count"] == 2

    def test_zero_up_is_a_different_case_entirely(self) -> None:
        """`network.all_links_down` owns this one; the MAJOR policy's EQ 1
        must not also match, or a disconnected server reports both.
        """
        facts = extract_facts(
            _server(interfaces=[NetworkInterface(name="a", link_state=LinkState.DOWN)])
        )
        assert facts["network.links_up_count"] == 0
