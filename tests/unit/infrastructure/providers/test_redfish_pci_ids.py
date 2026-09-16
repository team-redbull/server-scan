"""`..redfish.mapping._pci_ids_from_manufacturer` and `pcie_device_to_gpu`.

Confirmed live 2026-09-15: a BMC that reports GPUs only as `PCIeDevice`
packs the PCI-SIG vendor ID and device ID into `Manufacturer` as 8 hex
digits (`"10DE20B2"`) instead of a real name. See ADR-0016's 2026-09-15
GPU-model update for the PCI ID Repository sourcing.
"""

from __future__ import annotations

import pytest

from app.infrastructure.providers.redfish.mapping import (
    PcieGpuModelSpecError,
    _pci_ids_from_manufacturer,
    parse_pcie_gpu_models,
    pcie_device_to_gpu,
)


class TestPciIdsFromManufacturer:
    def test_splits_a_valid_8_hex_digit_string(self) -> None:
        assert _pci_ids_from_manufacturer("10DE20B2") == ("10de", "20b2")

    def test_is_case_insensitive(self) -> None:
        assert _pci_ids_from_manufacturer("10de20b2") == ("10de", "20b2")

    def test_strips_surrounding_whitespace(self) -> None:
        assert _pci_ids_from_manufacturer(" 10DE20B2 ") == ("10de", "20b2")

    def test_a_real_vendor_name_does_not_match(self) -> None:
        assert _pci_ids_from_manufacturer("NVIDIA Corporation") is None

    def test_wrong_length_does_not_match(self) -> None:
        assert _pci_ids_from_manufacturer("10DE20B") is None
        assert _pci_ids_from_manufacturer("10DE20B22") is None

    def test_non_hex_characters_do_not_match(self) -> None:
        assert _pci_ids_from_manufacturer("10DEZZZZ") is None


class TestPcieDeviceToGpuModel:
    def _device(self, manufacturer: str, description: str = "10DE VGA") -> dict[str, object]:
        return {
            "@odata.id": "/redfish/v1/Chassis/Self/PCIeDevices/0",
            "Description": description,
            "Manufacturer": manufacturer,
            "Status": {"State": "Enabled", "Health": "OK"},
        }

    def test_the_confirmed_live_device_id_resolves_to_a_catalog_alias(self) -> None:
        """10de:20b2 — confirmed live against the operator's own DGX-class
        fleet, cross-checked against the PCI ID Repository.
        """
        gpu = pcie_device_to_gpu(self._device("10DE20B2"))
        assert gpu["vendor"] == "NVIDIA"
        assert gpu["model"] == "A100-SXM4-80GB"

    def test_an_unlisted_device_id_falls_back_to_the_raw_description(self) -> None:
        gpu = pcie_device_to_gpu(self._device("10DEFFFF"))
        assert gpu["vendor"] == "NVIDIA"
        assert gpu["model"] == "10DE VGA"

    @pytest.mark.parametrize(
        ("manufacturer", "expected_model"),
        [
            ("10DE15F7", "Tesla P100-PCIE-12GB"),
            ("10DE15F8", "Tesla P100-PCIE-16GB"),
            ("10DE20BD", "A800-SXM4-40GB"),
            ("10DE20F3", "A800-SXM4-80GB"),
            ("10DE2237", "A10G"),
            ("10DE2322", "H800 PCIe"),
            ("10DE2324", "H800"),
        ],
    )
    def test_every_built_in_device_id_resolves(
        self, manufacturer: str, expected_model: str
    ) -> None:
        """Every ID added by ADR-0016's 2026-09-16 table expansion."""
        gpu = pcie_device_to_gpu(self._device(manufacturer))
        assert gpu["model"] == expected_model

    def test_a_resolved_model_enriches_vram_through_the_real_catalog(self) -> None:
        """End to end with the real `GpuCatalog`, not a stub — proves the
        resolved string is one it actually recognizes, not just a
        plausible-looking one.
        """
        from app.domain.value_objects.gpu_catalog import gpu_catalog

        gpu = pcie_device_to_gpu(self._device("10DE20BD"))
        enriched = gpu_catalog("").enrich(gpu)
        assert enriched["memory_bytes"] == 40 * 1024**3

    def test_an_unresearched_amd_device_id_falls_back_by_default(self) -> None:
        """AMD/Intel are not in the built-in table — see ADR-0016 — but
        the lookup itself is vendor-agnostic; `INVENTORY_REDFISH_PCIE_GPU_MODELS`
        can add one without a code change (see `TestParsePcieGpuModels`).
        """
        gpu = pcie_device_to_gpu(self._device("10020000", description="AMD VGA"))
        assert gpu["vendor"] == "AMD"
        assert gpu["model"] == "AMD VGA"

    def test_a_real_manufacturer_name_is_kept_as_the_model(self) -> None:
        """A BMC that reports a real name has no ID pair to resolve at all."""
        gpu = pcie_device_to_gpu(self._device("NVIDIA Corporation", description="A100 80GB"))
        assert gpu["model"] == "A100 80GB"

    def test_an_operator_override_resolves_an_unresearched_vendor(self) -> None:
        """`INVENTORY_REDFISH_PCIE_GPU_MODELS` closes exactly the AMD/Intel gap above."""
        gpu = pcie_device_to_gpu(
            self._device("10020000", description="AMD VGA"),
            pci_device_models={("1002", "0000"): "Instinct MI300X"},
        )
        assert gpu["model"] == "Instinct MI300X"

    def test_an_operator_override_wins_over_a_built_in_id(self) -> None:
        gpu = pcie_device_to_gpu(
            self._device("10DE20B2"),
            pci_device_models={("10de", "20b2"): "Custom Name"},
        )
        assert gpu["model"] == "Custom Name"


class TestParsePcieGpuModels:
    """`INVENTORY_REDFISH_PCIE_GPU_MODELS` — see ADR-0016's 2026-09-16 update."""

    def test_empty_spec_adds_nothing(self) -> None:
        assert parse_pcie_gpu_models("") == {}

    def test_one_entry(self) -> None:
        assert parse_pcie_gpu_models("10de:20f3:A800-SXM4-80GB") == {
            ("10de", "20f3"): "A800-SXM4-80GB"
        }

    def test_multiple_entries_are_comma_separated(self) -> None:
        spec = "10de:20f3:A800-SXM4-80GB,1002:74a1:Instinct MI300X"
        assert parse_pcie_gpu_models(spec) == {
            ("10de", "20f3"): "A800-SXM4-80GB",
            ("1002", "74a1"): "Instinct MI300X",
        }

    def test_is_case_insensitive_on_the_ids(self) -> None:
        assert parse_pcie_gpu_models("10DE:20F3:A800") == {("10de", "20f3"): "A800"}

    def test_whitespace_around_entries_and_fields_is_stripped(self) -> None:
        assert parse_pcie_gpu_models(" 10de : 20f3 : A800-SXM4-80GB , ") == {
            ("10de", "20f3"): "A800-SXM4-80GB"
        }

    def test_wrong_id_length_is_rejected(self) -> None:
        with pytest.raises(PcieGpuModelSpecError, match="4 hex digits"):
            parse_pcie_gpu_models("10de:2f3:A800")

    def test_non_hex_id_is_rejected(self) -> None:
        with pytest.raises(PcieGpuModelSpecError, match="not valid hex"):
            parse_pcie_gpu_models("10de:zzzz:A800")

    def test_missing_model_is_rejected(self) -> None:
        with pytest.raises(PcieGpuModelSpecError, match="names no model"):
            parse_pcie_gpu_models("10de:20f3:")

    def test_wrong_shape_is_rejected(self) -> None:
        with pytest.raises(PcieGpuModelSpecError, match="vendor_id:device_id:Model Name"):
            parse_pcie_gpu_models("10de-20f3-A800")
