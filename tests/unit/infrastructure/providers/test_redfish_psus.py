"""`..redfish.mapping.psus_from_supplies` — PSUs for every non-Cisco server.

UCS and Intersight populated `ProviderServer.psus`; Redfish did not, so
Dell, DGX and every standalone server reported no power supplies at all
and `power.failed_psu_count` was structurally zero for them.

What is tested here is the vocabulary and the absent-bay rule, because
both have already caused a silent wrong answer in this codebase once —
see the dated comments in `app.domain.services.health.facts`.
"""

from __future__ import annotations

from typing import Any

from app.infrastructure.providers.redfish.mapping import psus_from_supplies


def _supply(health: str | None, state: str | None, **extra: Any) -> dict[str, Any]:
    """
    One `PowerSupply` resource.

    Args:
        health (str | None): `Status.Health`.
        state (str | None): `Status.State`.
        **extra (Any): Any other resource fields.

    Returns:
        dict[str, Any]: The supply as a BMC would report it.
    """
    return {"Status": {"Health": health, "State": state}, **extra}


class TestVocabulary:
    """`Psu.health` is UP/DOWN/DISABLED/UNKNOWN, never HealthSeverity."""

    def test_a_working_supply_is_up(self) -> None:
        """`power.failed_psu_count` counts DOWN, so a healthy PSU must not
        land on any value that counts.
        """
        [psu] = psus_from_supplies([_supply("OK", "Enabled")]) or []
        assert psu["health"] == "UP"
        assert psu["health_detail"] == "OK/Enabled"

    def test_a_critical_supply_is_down(self) -> None:
        """The case the whole check exists for."""
        [psu] = psus_from_supplies([_supply("Critical", "Enabled")]) or []
        assert psu["health"] == "DOWN"

    def test_health_beats_state(self) -> None:
        """A supply can be `Enabled` and `Critical` at once — powered on and
        failed. Reading `State` alone would call that healthy.
        """
        [psu] = psus_from_supplies([_supply("Critical", "Enabled")]) or []
        assert psu["health"] == "DOWN"

    def test_a_warning_supply_is_not_counted_as_failed(self) -> None:
        """Degraded but still delivering power: counting it DOWN would raise
        CRITICAL on a server that has not lost redundancy. The raw pair is
        kept in `redfish_status` so a live run can settle it.
        """
        [psu] = psus_from_supplies([_supply("Warning", "Enabled")]) or []
        assert psu["health"] == "UNKNOWN"
        assert psu["redfish_status"] == "Warning/Enabled"
        # health_detail is the persisted counterpart of redfish_status.
        assert psu["health_detail"] == "Warning/Enabled"

    def test_an_offline_supply_is_down(self) -> None:
        """`UnavailableOffline` is a fitted supply that is not supplying."""
        [psu] = psus_from_supplies([_supply(None, "UnavailableOffline")]) or []
        assert psu["health"] == "DOWN"

    def test_an_administratively_disabled_supply_is_disabled(self) -> None:
        """Not a failure — someone turned it off."""
        [psu] = psus_from_supplies([_supply(None, "Disabled")]) or []
        assert psu["health"] == "DISABLED"


class TestAbsentBays:
    """An empty bay is not a failed PSU."""

    def test_an_absent_supply_is_dropped_entirely(self) -> None:
        """A 4-bay chassis with 2 supplies fitted is a 2-PSU server, not one
        with 2 failed PSUs — the same rule the Cisco collectors apply to an
        unequipped bay.
        """
        supplies = [
            _supply("OK", "Enabled"),
            _supply("OK", "Enabled"),
            _supply(None, "Absent"),
            _supply(None, "Absent"),
        ]
        psus = psus_from_supplies(supplies) or ()
        assert len(psus) == 2
        assert all(psu["health"] == "UP" for psu in psus)


class TestUnread:
    """None and () are different claims."""

    def test_unread_stays_none(self) -> None:
        """None propagates "could not read", which `_carry_forward` needs
        to keep already-stored PSUs rather than clearing them.
        """
        assert psus_from_supplies(None) is None

    def test_no_supplies_is_an_empty_tuple(self) -> None:
        """A chassis that genuinely reports none is not the same as one
        that could not be reached.
        """
        assert psus_from_supplies([]) == ()


class TestFields:
    """What reaches `app.domain.models.hardware.Psu`."""

    def test_identity_and_wattage_are_carried(self) -> None:
        """Wattage comes from either schema generation's spelling."""
        [psu] = (
            psus_from_supplies(
                [
                    _supply(
                        "OK",
                        "Enabled",
                        MemberId="0",
                        Model="PS-2112-9L",
                        SerialNumber="PH1234567890",
                        PowerCapacityWatts=1100,
                    )
                ]
            )
            or []
        )
        assert psu["id"] == "0"
        assert psu["model"] == "PS-2112-9L"
        assert psu["serial"] == "PH1234567890"
        assert psu["capacity_watts"] == 1100

    def test_the_newer_wattage_spelling_is_read_too(self) -> None:
        """`CapacityWatts` on PowerSubsystem-era resources,
        `PowerCapacityWatts` on the deprecated Power resource.
        """
        [psu] = psus_from_supplies([_supply("OK", "Enabled", CapacityWatts=800)]) or []
        assert psu["capacity_watts"] == 800


class TestPowerWatts:
    """`power_watts` reads `PowerSupplyMetrics.InputPowerWatts`, a
    separate linked resource — see ADR-0016's 2026-09-15 update.
    """

    def test_reads_input_power_from_the_linked_metrics(self) -> None:
        supply = _supply("OK", "Enabled", **{"@odata.id": "/redfish/v1/.../PSU1"})
        metrics = {
            "/redfish/v1/.../PSU1": {"InputPowerWatts": {"Reading": 245.0}},
        }
        [psu] = psus_from_supplies([supply], metrics_by_supply=metrics) or []
        assert psu["power_watts"] == 245.0

    def test_is_none_without_metrics(self) -> None:
        """No `metrics_by_supply` entry — the deprecated `Power` resource
        path has no `Metrics` link at all — degrades to unread, not 0.
        """
        [psu] = psus_from_supplies([_supply("OK", "Enabled")]) or []
        assert psu["power_watts"] is None
