"""`..redfish.mapping._pci_ids_from_manufacturer` and `pcie_device_to_gpu`.

Confirmed live 2026-09-15: a BMC that reports GPUs only as `PCIeDevice`
packs the PCI-SIG vendor ID and device ID into `Manufacturer` as 8 hex
digits (`"10DE20B2"`) instead of a real name. See ADR-0016's 2026-09-15
GPU-model update for the PCI ID Repository sourcing.
"""

from __future__ import annotations

from app.infrastructure.providers.redfish.mapping import (
    _pci_ids_from_manufacturer,
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

    def test_a_non_nvidia_vendor_id_is_never_looked_up(self) -> None:
        """AMD/Intel device IDs are not researched — see ADR-0016."""
        gpu = pcie_device_to_gpu(self._device("10020000", description="AMD VGA"))
        assert gpu["vendor"] == "AMD"
        assert gpu["model"] == "AMD VGA"

    def test_a_real_manufacturer_name_is_kept_as_the_model(self) -> None:
        """A BMC that reports a real name has no ID pair to resolve at all."""
        gpu = pcie_device_to_gpu(self._device("NVIDIA Corporation", description="A100 80GB"))
        assert gpu["model"] == "A100 80GB"
