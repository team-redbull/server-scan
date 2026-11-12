"""Deterministic fake confs for `tools.seed_inventory` (docs/adr/0061-confs.md).

Models and CPUs are the fake fleet's own (`generator._MODELS`/`_CPU_MODELS`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models.conf import (
    Conf,
    ConfAdapter,
    ConfCpu,
    ConfCpuCache,
    ConfDisk,
    ConfGpu,
    ConfRam,
)


@dataclass(frozen=True)
class _CpuSpec:
    """One CPU model's published figures; caches are per CPU, in KB."""

    vendor: str
    cores: int
    clock_mhz: int
    max_clock_mhz: int
    l1: int
    l2: int
    l3: int


_CPUS: dict[str, _CpuSpec] = {
    "Intel Xeon Gold 6338": _CpuSpec("Intel", 32, 2000, 3200, 2560, 40960, 49152),
    "Intel Xeon Platinum 8358": _CpuSpec("Intel", 32, 2600, 3400, 2560, 40960, 49152),
    "Intel Xeon Gold 6348": _CpuSpec("Intel", 28, 2600, 3500, 2240, 35840, 43008),
    "Intel Xeon Silver 4314": _CpuSpec("Intel", 16, 2400, 3400, 1280, 20480, 24576),
    "AMD EPYC 7513": _CpuSpec("AMD", 32, 2600, 3650, 2048, 16384, 131072),
    "AMD EPYC 9354": _CpuSpec("AMD", 32, 3250, 3800, 2048, 32768, 262144),
    "Intel Xeon Gold 6438Y+": _CpuSpec("Intel", 32, 2000, 4000, 2560, 65536, 61440),
    "AMD EPYC 9454": _CpuSpec("AMD", 48, 2750, 3800, 3072, 49152, 262144),
}

# (count, type, capacity_tb)
_DiskRow = tuple[int, str, float]
_BOOT_M2: _DiskRow = (2, "SSD", 0.48)
_BOOT_NVME: _DiskRow = (2, "NVME", 0.48)
_NVME_3_2: _DiskRow = (4, "NVME", 3.2)
_NVME_1_92: _DiskRow = (2, "NVME", 1.92)
_NVME_7_68: _DiskRow = (8, "NVME", 7.68)
_SATA_1_92: _DiskRow = (4, "SSD", 1.92)
_HDD_8: _DiskRow = (10, "HDD", 8.0)
_UNKNOWN_DISK: _DiskRow = (2, "unknown", 0.96)

# (ports, speed in Gb/s)
_CX6 = (2, 100.0)
_E810 = (2, 25.0)
_BCM_57414 = (2, 25.0)
_VIC = (4, 25.0)
_LOM = (2, 1.0)


@dataclass(frozen=True)
class _Row:
    """One fake conf, in the order the catalog lists it."""

    conf_id: str
    part_number: str | None
    model: str
    cpu: str
    sockets: int
    dimms: int
    per_dimm_gb: int
    ram_type: tuple[str, ...]
    disks: tuple[_DiskRow, ...]
    gpu: tuple[int, str, float] | None
    adapters: tuple[tuple[int, tuple[int, float]], ...]


_DDR4 = ("DDR4",)
_DDR5 = ("DDR5",)

# `CONF-<n>` is a scan id with no catalog entry, so it has no part number;
# `CONF-34`'s memory scan failed, so its RAM is all zeros.
_ROWS: tuple[_Row, ...] = (
    _Row("CONF200", "210-AZKL", "PowerEdge R650", "Intel Xeon Gold 6338",
         2, 16, 32, _DDR4, (_BOOT_M2, _NVME_1_92), None, ((1, _E810), (1, _LOM))),
    _Row("CONF200.2", "210-AZKL-2", "PowerEdge R650",
         "Intel Xeon Gold 6338", 2, 16, 64, _DDR4, (_BOOT_M2, _NVME_1_92), None,
         ((1, _E810), (1, _LOM))),
    _Row("CONF201", "210-AZYV", "PowerEdge R750", "Intel Xeon Platinum 8358",
         2, 32, 64, _DDR4, (_BOOT_M2, _NVME_3_2), None, ((2, _CX6), (1, _LOM))),
    _Row("CONF202", "210-AZYV-G", "PowerEdge R750", "Intel Xeon Platinum 8358",
         2, 32, 64, _DDR4, (_BOOT_M2, _NVME_7_68), (8, "NVIDIA H200 SXM", 141), ((4, _CX6),)),
    _Row("CONF203", "210-ASVF", "PowerEdge R6515", "AMD EPYC 7513",
         1, 8, 32, _DDR4, (_BOOT_M2,), None, ((1, _BCM_57414),)),
    _Row("CONF210", "UCSC-C220-M6S", "UCS C220 M6", "Intel Xeon Gold 6348",
         2, 16, 32, _DDR4, (_BOOT_NVME, _SATA_1_92), None, ((1, _VIC),)),
    _Row("CONF211", "UCSC-C240-M6SX", "UCS C240 M6", "Intel Xeon Gold 6348",
         2, 24, 32, _DDR4, (_BOOT_NVME, _HDD_8), None, ((1, _VIC), (1, _CX6))),
    _Row("CONF211.2", "UCSC-C240-M6SX-G", "UCS C240 M6",
         "Intel Xeon Gold 6348", 2, 24, 32, _DDR4, (_BOOT_NVME, _NVME_1_92), (2, "NVIDIA L4", 24),
         ((1, _VIC),)),
    _Row("CONF212", "UCSB-B200-M6", "UCS B200 M6", "Intel Xeon Silver 4314",
         2, 16, 32, _DDR4, (_BOOT_M2,), None, ((1, _VIC),)),
    _Row("CONF220", "P40636-B21", "ProLiant DL360 Gen11",
         "AMD EPYC 9354", 2, 24, 32, _DDR5, (_BOOT_M2, _NVME_3_2), None, ((1, _E810),)),
    _Row("CONF221", "P52534-B21", "ProLiant DL380 Gen11", "AMD EPYC 9354",
         2, 24, 64, _DDR5, (_BOOT_M2, _NVME_7_68), None, ((2, _CX6),)),
    _Row("CONF221.2", "P52534-B21-G", "ProLiant DL380 Gen11",
         "AMD EPYC 9354", 2, 24, 64, _DDR5, (_BOOT_M2, _NVME_7_68), (4, "NVIDIA H100", 80),
         ((2, _CX6),)),
    _Row("CONF222", "P54199-B21", "ProLiant DL325 Gen11", "AMD EPYC 9354",
         1, 12, 32, _DDR5, (_BOOT_M2,), None, ((1, _BCM_57414),)),
    _Row("CONF223", "868703-B21", "ProLiant DL380 Gen10", "AMD EPYC 7513",
         2, 16, 32, _DDR4, (_BOOT_NVME, _SATA_1_92), None, ((1, _BCM_57414),)),
    _Row("CONF224", "867959-B21", "ProLiant DL360 Gen10", "AMD EPYC 7513",
         2, 12, 32, _DDR4, (_BOOT_NVME,), None, ((1, _BCM_57414),)),
    _Row("CONF230", "7D76CTO1WW", "ThinkSystem SR650 V3",
         "Intel Xeon Gold 6438Y+", 2, 16, 64, _DDR5, (_BOOT_M2, _NVME_3_2), None, ((1, _E810),)),
    _Row("CONF231", "SYS-221H-TNR", "SYS-221H-TNR",
         "Intel Xeon Gold 6438Y+", 2, 16, 64, _DDR5, (_BOOT_M2, _NVME_7_68), (4, "NVIDIA L40S", 48),
         ((2, _CX6),)),
    _Row("CONF232", "AS-2125HS-TNR", "AS-2125HS-TNR", "AMD EPYC 9454",
         2, 24, 96, _DDR5, (_BOOT_M2, _NVME_7_68), None, ((2, _CX6),)),
    _Row("CONF-21", None, "PowerEdge R650", "Intel Xeon Gold 6338",
         2, 16, 32, (), (_BOOT_M2, _UNKNOWN_DISK), None, ((1, _E810),)),
    _Row("CONF-34", None, "ProLiant DL380 Gen9", "AMD EPYC 7513",
         2, 0, 0, (), (_UNKNOWN_DISK,), None, ((1, _LOM),)),
)  # fmt: skip


def _conf(row: _Row) -> Conf:
    """
    Build one conf from its row.

    Args:
        row (_Row): The handcrafted spec.

    Returns:
        Conf: The conf as the writer stores it.
    """
    cpu = _CPUS[row.cpu]
    gpu_units, gpu_model, gpu_vram_gb = row.gpu or (0, "", 0)
    return Conf(
        conf_id=row.conf_id,
        part_number=row.part_number,
        model=row.model,
        cpu=ConfCpu(
            model=row.cpu,
            vendor=cpu.vendor,
            sockets=row.sockets,
            cores_per_cpu=cpu.cores,
            total_cores_ht=row.sockets * cpu.cores * 2,
            clock_mhz=cpu.clock_mhz,
            max_clock_mhz=cpu.max_clock_mhz,
            cache_kb=ConfCpuCache(l1=cpu.l1, l2=cpu.l2, l3=cpu.l3),
        ),
        ram=ConfRam(
            total_gib=row.dimms * row.per_dimm_gb,
            dimms=row.dimms,
            per_dimm_gb=row.per_dimm_gb,
            type=list(row.ram_type),
        ),
        disks=[ConfDisk(count=count, type=kind, capacity_tb=tb) for count, kind, tb in row.disks],
        gpus=[ConfGpu(count=gpu_units, model=gpu_model)] if gpu_units else [],
        gpu_count=gpu_units,
        gpu_vram_gb=gpu_vram_gb,
        adapters=[
            ConfAdapter(count=count, ports=ports, speed=speed)
            for count, (ports, speed) in row.adapters
        ],
        adapter_count=sum(count for count, _ in row.adapters),
    )


def generate_confs() -> list[Conf]:
    """
    Build the fake conf catalog; it is fixed, so every seed and fleet size gets the same one.

    Returns:
        list[Conf]: One conf per row.
    """
    return [_conf(row) for row in _ROWS]
