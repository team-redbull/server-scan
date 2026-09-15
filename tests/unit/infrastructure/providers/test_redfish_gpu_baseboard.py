"""`..redfish.mapping.has_only_gpu_processors` — DGX/HGX tray detection.

Confirmed live 2026-09-15: a real GPU-baseboard tray reported 8 GPUs plus
an FPGA `Processor` entry, which the original "every processor is a GPU"
rule rejected as a tray. The revised rule ("at least one GPU, no CPU")
still has to reject a normal CPU host with an add-in GPU card — including
one whose CPU entries omit `ProcessorType` entirely, which
`cpu_summary` already treats as a CPU by convention (docs/adr/0016).
"""

from __future__ import annotations

from typing import Any

from app.infrastructure.providers.redfish.mapping import has_only_gpu_processors


def _processor(processor_type: str | None, **extra: Any) -> dict[str, Any]:
    """
    One `Processor` resource.

    Args:
        processor_type (str | None): `ProcessorType`, or `None` to omit
            the field entirely.
        **extra (Any): Any other resource fields.

    Returns:
        dict[str, Any]: The processor as a BMC would report it.
    """
    resource: dict[str, Any] = {"Status": {"State": "Enabled", "Health": "OK"}, **extra}
    if processor_type is not None:
        resource["ProcessorType"] = processor_type
    return resource


class TestHasOnlyGpuProcessors:
    def test_all_gpu_is_a_tray(self) -> None:
        assert has_only_gpu_processors([_processor("GPU"), _processor("GPU")]) is True

    def test_a_gpu_plus_an_fpga_is_still_a_tray(self) -> None:
        """The exact live-confirmed shape: 8 GPUs plus one non-CPU companion."""
        assert has_only_gpu_processors([_processor("GPU"), _processor("FPGA")]) is True

    def test_a_gpu_plus_a_cpu_is_not_a_tray(self) -> None:
        assert has_only_gpu_processors([_processor("GPU"), _processor("CPU")]) is False

    def test_a_cpu_with_no_processor_type_is_still_a_cpu(self) -> None:
        """`cpu_summary` defaults a missing `ProcessorType` to CPU; a normal
        host with unmarked CPUs and one GPU add-in card must not
        misclassify as an all-GPU tray.
        """
        assert has_only_gpu_processors([_processor(None), _processor("GPU")]) is False

    def test_only_fpgas_with_no_gpu_is_not_a_tray(self) -> None:
        assert has_only_gpu_processors([_processor("FPGA"), _processor("FPGA")]) is False

    def test_an_absent_cpu_slot_does_not_count(self) -> None:
        """An unequipped bay must not make a real GPU tray look like it has a CPU."""
        absent_cpu = _processor("CPU", Status={"State": "Absent", "Health": None})
        assert has_only_gpu_processors([_processor("GPU"), absent_cpu]) is True

    def test_none_or_empty_processors_is_not_a_tray(self) -> None:
        assert has_only_gpu_processors(None) is False
        assert has_only_gpu_processors([]) is False
