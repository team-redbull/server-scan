"""The built-in datacenter GPU table `GpuCatalog` ships as its default.

Data only — no logic, no I/O, no imports beyond the standard library.
The matching rules that consume this table live in
`app.domain.value_objects.gpu_catalog`; they change rarely, this table
changes every time a card ships. Splitting them keeps a routine "add a
row" edit away from the code that decides what a match means.

Every row's VRAM figure comes from a primary vendor source — an NVIDIA
or AMD datasheet, or a Cisco UCS spec sheet — and the sourcing standard
for adding one is
`docs/adr/0021-built-in-gpu-catalog-with-model-matching.md`, which also
lists the per-family source URLs. A card whose VRAM could not be sourced
is deliberately absent rather than guessed; an operator adds it with
`INVENTORY_GPU_MODELS`.
"""

from __future__ import annotations

DEFAULT_GPU_MODELS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    # (friendly name, VRAM in GB, identifiers this row matches). A bare
    # model name keys only a single-capacity card — ADR-0021, decisions 2-3.
    # --- NVIDIA, Pascal ---
    ("NVIDIA Tesla P100 12GB", 12, ("UCSC-GPU-P100-12G", "Tesla P100-PCIE-12GB", "P100-12GB")),
    ("NVIDIA Tesla P100 16GB", 16, ("UCSC-GPU-P100-16G", "Tesla P100-PCIE-16GB", "P100-16GB")),
    # --- NVIDIA, Volta ---
    (
        "NVIDIA Tesla V100 16GB",
        16,
        ("UCSC-GPU-V100", "Tesla V100-PCIE-16GB", "Tesla V100-SXM2-16GB", "V100-16GB"),
    ),
    (
        "NVIDIA Tesla V100 32GB",
        32,
        (
            "UCSC-GPU-V100-32",
            "Tesla V100-PCIE-32GB",
            "Tesla V100-SXM2-32GB",
            "Tesla V100S-PCIE-32GB",
            "V100-32GB",
        ),
    ),
    # --- NVIDIA, Turing ---
    ("NVIDIA T4 16GB", 16, ("UCSC-GPU-T4-16", "UCSX-GPU-T4-16", "Tesla T4", "T4")),
    ("NVIDIA Quadro RTX 6000 24GB", 24, ("UCSC-GPU-RTX6000", "Quadro RTX 6000")),
    ("NVIDIA Quadro RTX 8000 48GB", 48, ("UCSC-GPU-RTX8000", "Quadro RTX 8000")),
    # --- NVIDIA, Ampere ---
    ("NVIDIA A2 16GB", 16, ("A2",)),
    ("NVIDIA A10 24GB", 24, ("UCSC-GPU-A10", "A10")),
    # One card, four 16GB GPUs; the row is per GPU — ADR-0021, "Consequences".
    (
        "NVIDIA A16 16GB",
        16,
        (
            "UCSC-GPU-A16",
            "UCSC-GPU-A16-D",
            "UCSC-GPU-A165",
            "UCSX-GPU-A16",
            "UCSX-GPU-A16-D",
            "A16",
        ),
    ),
    ("NVIDIA A30 24GB", 24, ("UCSC-GPU-A30", "UCSC-GPU-A30-D", "A30")),
    ("NVIDIA A40 48GB", 48, ("UCSC-GPU-A40", "UCSC-GPU-A40-D", "UCSX-GPU-A40", "A40")),
    (
        "NVIDIA A100 40GB",
        40,
        (
            "UCSC-GPU-A100",
            "A100-PCIE-40GB",
            "A100-SXM4-40GB",
            "A100 40GB PCIe",
            "A100 40GB",
        ),
    ),
    (
        "NVIDIA A100 80GB",
        80,
        (
            "UCSC-GPU-A100-80",
            "UCSC-GPU-A100-805",
            "UCSC-GPUA100-80-D",
            "UCSX-GPU-A100-80",
            "UCSX-GPU-A100-80-D",
            "A100-PCIE-80GB",
            "A100-SXM4-80GB",
            "A100 80GB PCIe",
            "A100 80GB SXM",
            "A100 80GB",
        ),
    ),
    # --- NVIDIA, Ada Lovelace ---
    ("NVIDIA L4 24GB", 24, ("UCSC-GPU-L4", "UCSC-GPU-L4M6", "UCSX-GPU-L4", "L4")),
    ("NVIDIA L40 48GB", 48, ("UCSC-GPU-L40", "UCSX-GPU-L40", "L40")),
    ("NVIDIA L40S 48GB", 48, ("UCSC-GPU-L40S", "UCSX-GPU-L40S", "L40S")),
    # --- NVIDIA, Hopper ---
    (
        "NVIDIA H100 80GB",
        80,
        (
            "UCSC-GPU-H100-80",
            "UCSX-GPU-H100-80",
            "H100-PCIE-80GB",
            "H100-SXM5-80GB",
            "H100 80GB HBM3",
            "H100 PCIe",
            "H100 80GB",
        ),
    ),
    (
        "NVIDIA H100 NVL 94GB",
        94,
        ("UCSC-GPU-H100-NVL", "UCSX-GPU-H100-NVL", "H100 NVL", "H100 NVL 94GB"),
    ),
    ("NVIDIA H200 141GB", 141, ("H200", "H200 NVL", "H200-SXM-141GB", "H200 141GB")),
    # --- NVIDIA, Blackwell ---
    ("NVIDIA B200 180GB", 180, ("B200", "B200 180GB")),
    (
        "NVIDIA RTX PRO 6000 Blackwell 96GB",
        96,
        ("RTX PRO 6000 Blackwell Server Edition", "RTX PRO 6000 Blackwell"),
    ),
    # --- AMD Instinct ---
    ("AMD Instinct MI100 32GB", 32, ("Instinct MI100", "MI100")),
    ("AMD Instinct MI210 64GB", 64, ("UCSX-GPU-MI210", "Instinct MI210", "MI210")),
    ("AMD Instinct MI250 128GB", 128, ("Instinct MI250", "MI250")),
    ("AMD Instinct MI250X 128GB", 128, ("Instinct MI250X", "MI250X")),
    ("AMD Instinct MI300A 128GB", 128, ("Instinct MI300A", "MI300A")),
    ("AMD Instinct MI300X 192GB", 192, ("Instinct MI300X", "MI300X")),
    ("AMD Instinct MI325X 256GB", 256, ("Instinct MI325X", "MI325X")),
    ("AMD Instinct MI355X 288GB", 288, ("Instinct MI355X", "MI355X")),
)
