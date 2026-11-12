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

# (count, model, capacity_tb, type, protocol)
_DiskRow = tuple[int, str, float, str, str]
_BOOT_M2: _DiskRow = (2, "MTFDDAV480TDS", 0.48, "SSD", "SATA")
_BOOT_SATA: _DiskRow = (2, "SAMSUNG MZ7L3480HCHQ", 0.48, "SSD", "SATA")
_NVME_3_2: _DiskRow = (4, "Dell Ent NVMe CM6 MU 3.2TB", 3.2, "SSD", "PCIe")
_NVME_1_92: _DiskRow = (2, "SAMSUNG MZQL21T9HCJR", 1.92, "SSD", "PCIe")
_NVME_7_68: _DiskRow = (8, "KIOXIA KCD8XRUG7T68", 7.68, "SSD", "PCIe")
_SATA_1_92: _DiskRow = (4, "SAMSUNG MZ7L31T9HBLT-00A07", 1.92, "SSD", "SATA")
_HDD_8: _DiskRow = (10, "ST8000NM017B", 8.0, "HDD", "SATA")
_UNKNOWN_DISK: _DiskRow = (2, "unknown", 0.96, "unknown", "unknown")

_CX6 = "MLNX 100GbE 2P ConnectX6 Adpt"
_E810 = "Intel E810-XXVDA2 25GbE 2P Adapter"
_BCM_57414 = "Broadcom 57414 Dual Port 10/25GbE SFP28 Adapter"
_VIC = "Cisco UCS VIC 1467 4P 10/25GbE"
_LOM = "BCM 5720 1GbE 2P LOM"


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
    adapters: tuple[tuple[int, str], ...]


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
         2, 32, 64, _DDR4, (_BOOT_M2, _NVME_7_68), (8, "NVIDIA H200", 141), ((4, _CX6),)),
    _Row("CONF203", "210-ASVF", "PowerEdge R6515", "AMD EPYC 7513",
         1, 8, 32, _DDR4, (_BOOT_M2,), None, ((1, _BCM_57414),)),
    _Row("CONF210", "UCSC-C220-M6S", "UCS C220 M6", "Intel Xeon Gold 6348",
         2, 16, 32, _DDR4, (_BOOT_SATA, _SATA_1_92), None, ((1, _VIC),)),
    _Row("CONF211", "UCSC-C240-M6SX", "UCS C240 M6", "Intel Xeon Gold 6348",
         2, 24, 32, _DDR4, (_BOOT_SATA, _HDD_8), None, ((1, _VIC), (1, _CX6))),
    _Row("CONF211.2", "UCSC-C240-M6SX-G", "UCS C240 M6",
         "Intel Xeon Gold 6348", 2, 24, 32, _DDR4, (_BOOT_SATA, _NVME_1_92), (2, "NVIDIA L4", 24),
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
         2, 16, 32, _DDR4, (_BOOT_SATA, _SATA_1_92), None, ((1, _BCM_57414),)),
    _Row("CONF224", "867959-B21", "ProLiant DL360 Gen10", "AMD EPYC 7513",
         2, 12, 32, _DDR4, (_BOOT_SATA,), None, ((1, _BCM_57414),)),
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
    gpu_units, gpu_model, gpu_vram_gb = row.gpu or (0, "", None)
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
        disks=[
            ConfDisk(count=count, model=model, capacity_tb=tb, type=kind, protocol=protocol)
            for count, model, tb, kind, protocol in row.disks
        ],
        # One entry per scanned unit, as the writer stores them.
        gpus=[ConfGpu(count=1, model=gpu_model) for _ in range(gpu_units)],
        gpu_count=gpu_units,
        gpu_vram_gb=gpu_vram_gb,
        adapters=[ConfAdapter(count=count, model=model) for count, model in row.adapters],
        adapter_count=sum(count for count, _ in row.adapters),
    )


def generate_confs() -> list[Conf]:
    """
    Build the fake conf catalog; it is fixed, so every seed and fleet size gets the same one.

    Returns:
        list[Conf]: One conf per row.
    """
    return [_conf(row) for row in _ROWS]
