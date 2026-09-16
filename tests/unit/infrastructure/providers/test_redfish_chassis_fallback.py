"""`..redfish.mapping._model`/`_chassis_serial` — some BMCs report
`ComputerSystem.Model`/`SerialNumber` as empty or whitespace-only while
`Chassis.ProductName`/`SerialNumber` carry the real values, confirmed
live (docs/adr/0016, 2026-09-16 update).
"""

from __future__ import annotations

from app.infrastructure.providers.redfish.mapping import _chassis_serial, _model


def _chassis(*, serial: str | None = None, systems: int = 1) -> dict[str, object]:
    return {
        "ProductName": "DGX H100",
        "SerialNumber": serial,
        "Links": {"ComputerSystems": [{"@odata.id": f"/s/{i}"} for i in range(systems)]},
    }


class TestModelFallback:
    def test_a_real_model_wins_even_with_a_chassis_name_available(self) -> None:
        system = {"Model": "PowerEdge R660"}
        assert _model(system, _chassis()) == "PowerEdge R660"

    def test_falls_back_to_the_chassis_product_name_when_model_is_absent(self) -> None:
        assert _model({}, _chassis()) == "DGX H100"

    def test_falls_back_when_model_is_an_empty_string(self) -> None:
        assert _model({"Model": ""}, _chassis()) == "DGX H100"

    def test_falls_back_when_model_is_whitespace_only(self) -> None:
        assert _model({"Model": "   "}, _chassis()) == "DGX H100"

    def test_none_when_neither_source_has_a_model(self) -> None:
        assert _model({}, None) is None
        assert _model({"Model": ""}, None) is None

    def test_strips_the_model_it_reads(self) -> None:
        assert _model({"Model": "  PowerEdge R660  "}, None) == "PowerEdge R660"


class TestChassisSerial:
    def test_reads_the_chassis_serial_when_it_wholly_contains_one_system(self) -> None:
        assert _chassis_serial(_chassis(serial="88712345000678901B", systems=1)) == (
            "88712345000678901B"
        )

    def test_none_without_a_chassis(self) -> None:
        assert _chassis_serial(None) is None

    def test_none_when_the_chassis_serial_is_itself_blank_or_a_placeholder(self) -> None:
        assert _chassis_serial(_chassis(serial=None)) is None
        assert _chassis_serial(_chassis(serial="")) is None
        assert _chassis_serial(_chassis(serial="Not Specified")) is None

    def test_none_when_the_chassis_wholly_contains_more_than_one_system(self) -> None:
        """A shared enclosure's own serial cannot be attributed to one of
        several systems it contains (DMTF Chassis.Links.ComputerSystems)."""
        assert _chassis_serial(_chassis(serial="88712345000678901B", systems=2)) is None

    def test_trusts_it_when_the_reverse_link_is_simply_absent(self) -> None:
        chassis = {"SerialNumber": "88712345000678901B"}
        assert _chassis_serial(chassis) == "88712345000678901B"
