"""The `Conf` document: one server configuration (spec) in the `confs` collection.

Server Scan only reads this collection; its shape is the writer's, reproduced
field for field (docs/adr/0061-confs.md), so do not tidy it here.
"""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field


class ConfCpuCache(BaseModel):
    """Per-level CPU cache sizes, in KB; stored as `l1`/`l2`/`l3` or `11`/`12`/`13` (ADR-0061)."""

    l1: int = Field(validation_alias=AliasChoices("l1", "11"))
    l2: int = Field(validation_alias=AliasChoices("l2", "12"))
    l3: int = Field(validation_alias=AliasChoices("l3", "13"))


class ConfCpu(BaseModel):
    """The processors a conf carries."""

    model: str
    vendor: str | None = None
    sockets: int
    cores_per_cpu: int
    total_cores_ht: int
    clock_mhz: int
    max_clock_mhz: int
    cache_kb: ConfCpuCache


class ConfRam(BaseModel):
    """The memory a conf carries; `total_gib` 0 is a failed scan, `type` `[]` a failed DIMM read."""

    total_gib: int
    dimms: int
    per_dimm_gb: int
    type: list[str] = Field(default_factory=list)


class ConfDisk(BaseModel):
    """One group of drives of one type and size; the writer records no drive model."""

    count: int
    type: str
    capacity_tb: float


class ConfGpu(BaseModel):
    """One group of identical GPUs."""

    count: int
    model: str


class ConfAdapter(BaseModel):
    """One group of network adapters with the same port count and speed (Gb/s, ADR-0061)."""

    count: int
    ports: int
    speed: float


class Conf(BaseModel):
    """One server configuration (spec)."""

    conf_id: str
    part_number: str | None = None
    model: str
    cpu: ConfCpu
    ram: ConfRam
    disks: list[ConfDisk] = Field(default_factory=list)
    gpus: list[ConfGpu] = Field(default_factory=list)
    gpu_count: int = 0
    gpu_vram_gb: float | None = None
    adapters: list[ConfAdapter] = Field(default_factory=list)
    adapter_count: int = 0
