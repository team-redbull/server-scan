"""The two gates `GET /servers/available` applies to a candidate, unit-level.

`nic_mac_filters` builds the Mongo clauses and `server_still_qualifies` re-checks
the same conditions after a live recheck. They must agree: a candidate the query
returned but the predicate rejects is drawn and discarded on every round.

Why either gate exists, given the health tiers already filter: a server whose
NIC read failed has no network facts to fail a policy on, so its network
category is `UNKNOWN`, which ranks below `HEALTHY` and never lowers the rollup —
it reads `HEALTHY` overall. ADR-0032's 2026-09-25 update has the measurements.
The network-category gate closes that directly by reading `health.network`
rather than `health.overall`; the MAC gate remains a separate, cruder check on
`identity.nic_macs`, which is a different list (NPAR-unreduced).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.services.available_servers import (
    SELECTABLE_TIERS,
    nic_mac_filters,
    server_still_qualifies,
)
from app.domain.enums import HealthSeverity, OpenShiftState, Vendor
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 25, tzinfo=UTC)


def _server(
    *,
    nic_macs: list[str] | None = None,
    unread_fields: list[str] | None = None,
    health: HealthSeverity = HealthSeverity.HEALTHY,
    network: HealthSeverity = HealthSeverity.HEALTHY,
    lifecycle: OpenShiftState = OpenShiftState.AVAILABLE,
    reachable: bool = True,
    maintenance: bool = False,
) -> Server:
    return Server(
        _id="srv_test",
        name="ocp-dell-r650-tlv-64c-1024gb-DEL0000485",
        name_normalized="ocp-dell-r650-tlv-64c-1024gb-del0000485",
        identity=Identity(vendor=Vendor.DELL, nic_macs=nic_macs or []),
        health=Health(overall=health, network=network),
        openshift=OpenShiftLifecycle(lifecycle_state=lifecycle),
        maintenance=Maintenance(enabled=maintenance),
        reachable=reachable,
        unread_fields=unread_fields or [],
        created_at=_NOW,
        updated_at=_NOW,
    )


class TestNicMacFilters:
    def test_a_floor_of_one_asks_for_a_first_element_and_a_real_reading(self) -> None:
        assert nic_mac_filters(1) == {
            "identity.nic_macs.0": {"$exists": True},
            "unread_fields": {"$ne": "identity.nic_macs"},
        }

    def test_a_bond_asks_for_a_second_element(self) -> None:
        # Dotted-index existence is Mongo's array-length-at-least test.
        assert nic_mac_filters(2)["identity.nic_macs.1"] == {"$exists": True}

    def test_zero_imposes_nothing(self) -> None:
        # The default: the endpoint's existing behaviour is unchanged unless a
        # caller asks for the gate.
        assert nic_mac_filters(0) == {}


class TestServerStillQualifies:
    def test_a_healthy_assignable_server_with_macs_qualifies(self) -> None:
        assert server_still_qualifies(_server(nic_macs=["aa:bb:cc:dd:ee:01"]))

    def test_a_server_with_no_macs_is_rejected_when_the_gate_is_asked_for(self) -> None:
        # It reads HEALTHY — that is exactly the case this gate exists for.
        # The gate is opt-in, so the default still admits it.
        macless = _server(nic_macs=[])
        assert server_still_qualifies(macless)
        assert not server_still_qualifies(macless, min_nic_macs=1)

    def test_the_default_draw_and_this_predicate_admit_the_same_servers(self) -> None:
        # A disagreement here silently narrows the endpoint's default and
        # burns a replacement round per draw — ADR-0032's 2026-09-26 update.
        unread = _server(nic_macs=[], unread_fields=["identity.nic_macs"])
        assert nic_mac_filters(0) == {}
        assert server_still_qualifies(unread)

    def test_carried_forward_macs_are_rejected_when_the_read_failed(self) -> None:
        # The count alone is satisfied; only unread_fields reveals that the
        # values came from an earlier run rather than this one.
        server = _server(
            nic_macs=["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
            unread_fields=["identity.nic_macs"],
        )
        assert not server_still_qualifies(server, min_nic_macs=2)

    def test_one_mac_does_not_satisfy_a_bond(self) -> None:
        assert not server_still_qualifies(_server(nic_macs=["aa:bb:cc:dd:ee:01"]), min_nic_macs=2)

    def test_an_unrelated_unread_field_does_not_disqualify(self) -> None:
        server = _server(nic_macs=["aa:bb:cc:dd:ee:01"], unread_fields=["hardware.gpus"])
        assert server_still_qualifies(server)

    @pytest.mark.parametrize("health", [HealthSeverity.WARNING, HealthSeverity.MAJOR])
    def test_a_narrowed_tier_list_rejects_lesser_tiers(self, health: HealthSeverity) -> None:
        server = _server(nic_macs=["aa:bb:cc:dd:ee:01"], health=health)
        # Assignable by the platform's default, but not to a HEALTHY-only caller.
        assert server_still_qualifies(server)
        assert not server_still_qualifies(server, tiers=(HealthSeverity.HEALTHY,))

    @pytest.mark.parametrize("health", [HealthSeverity.CRITICAL, HealthSeverity.UNKNOWN])
    def test_critical_and_unknown_are_never_assignable(self, health: HealthSeverity) -> None:
        assert health not in SELECTABLE_TIERS
        assert not server_still_qualifies(_server(nic_macs=["aa:bb:cc:dd:ee:01"], health=health))

    def test_a_claimed_unreachable_or_maintained_server_is_rejected(self) -> None:
        macs = ["aa:bb:cc:dd:ee:01"]
        assert not server_still_qualifies(
            _server(nic_macs=macs, lifecycle=OpenShiftState.INSTALLED)
        )
        assert not server_still_qualifies(_server(nic_macs=macs, reachable=False))
        assert not server_still_qualifies(_server(nic_macs=macs, maintenance=True))


class TestNetworkCategoryGate:
    """`health.overall` cannot express "the links were actually read"."""

    def test_an_unread_network_category_is_rejected_though_overall_is_healthy(
        self,
    ) -> None:
        """The hole this closes, and the reason it cannot live in the tiers.

        OneView carries no link state and most Intersight vNICs report none,
        so those servers roll up HEALTHY off their other categories.
        """
        server = _server(
            nic_macs=["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
            health=HealthSeverity.HEALTHY,
            network=HealthSeverity.UNKNOWN,
        )
        assert server.health.overall is HealthSeverity.HEALTHY
        assert not server_still_qualifies(server)

    def test_a_failing_network_category_is_rejected(self) -> None:
        assert not server_still_qualifies(
            _server(network=HealthSeverity.CRITICAL, health=HealthSeverity.CRITICAL)
        )

    def test_a_read_and_passing_network_category_qualifies(self) -> None:
        assert server_still_qualifies(_server(network=HealthSeverity.HEALTHY))

    def test_a_widened_tier_does_not_widen_the_network_requirement(self) -> None:
        """`?health=` picks how bad the ROLLUP may be. A WARNING server with a
        good network is installable; an unread network is not, at any tier.
        """
        assert server_still_qualifies(
            _server(health=HealthSeverity.WARNING, network=HealthSeverity.HEALTHY)
        )
        assert not server_still_qualifies(
            _server(health=HealthSeverity.WARNING, network=HealthSeverity.UNKNOWN)
        )
