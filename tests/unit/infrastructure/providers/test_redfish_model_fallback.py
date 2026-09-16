"""`..redfish.mapping._model` — some BMCs report `ComputerSystem.Model` as
empty or whitespace-only while `Chassis.ProductName` carries the real
value, confirmed live (docs/adr/0016, 2026-09-16 update).
"""

from __future__ import annotations

from app.infrastructure.providers.redfish.mapping import _model


class TestModelFallback:
    def test_a_real_model_wins_even_with_a_chassis_name_available(self) -> None:
        system = {"Model": "PowerEdge R660"}
        assert _model(system, "Different Product Name") == "PowerEdge R660"

    def test_falls_back_to_the_chassis_product_name_when_model_is_absent(self) -> None:
        assert _model({}, "DGX H100") == "DGX H100"

    def test_falls_back_when_model_is_an_empty_string(self) -> None:
        assert _model({"Model": ""}, "DGX H100") == "DGX H100"

    def test_falls_back_when_model_is_whitespace_only(self) -> None:
        assert _model({"Model": "   "}, "DGX H100") == "DGX H100"

    def test_none_when_neither_source_has_a_model(self) -> None:
        assert _model({}, None) is None
        assert _model({"Model": ""}, None) is None

    def test_strips_the_model_it_reads(self) -> None:
        assert _model({"Model": "  PowerEdge R660  "}, None) == "PowerEdge R660"
