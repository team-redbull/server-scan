"""Vendor-neutral hardware model.

Arrays for variable-cardinality hardware (drives, DIMMs, GPUs, PSUs, NICs)
per the platform spec — a server may have 0, 1, or many of any of these,
and nothing here assumes a fixed count.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import MediaType


class Cpu(BaseModel):
    """A server's aggregate CPU configuration."""

    sockets: int = 0
    cores: int = 0
    threads: int = 0
    model: str | None = None


class MemoryModule(BaseModel):
    """One DIMM reported for a server."""

    slot: str | None = None
    size_bytes: int | None = None
    type: str | None = None
    speed_mhz: int | None = None
    serial: str | None = None
    health: str | None = None
    health_detail: str | None = None


class Memory(BaseModel):
    """A server's aggregate memory configuration and its individual DIMMs."""

    total_bytes: int = 0
    modules: list[MemoryModule] = Field(default_factory=list)


class StorageDrive(BaseModel):
    """One storage drive reported for a server."""

    id: str
    model: str | None = None
    serial: str | None = None
    media_type: MediaType = MediaType.UNKNOWN
    protocol: str | None = None
    capacity_bytes: int | None = None
    slot: str | None = None
    health: str | None = None
    health_detail: str | None = None
    firmware_version: str | None = None
    controller_id: str | None = None


class Storage(BaseModel):
    """A server's aggregate storage capacity and its individual drives."""

    total_bytes: int = 0
    drives: list[StorageDrive] = Field(default_factory=list)


class Gpu(BaseModel):
    """One GPU reported for a server."""

    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    memory_bytes: int | None = None
    health: str | None = None
    health_detail: str | None = None
    pci_address: str | None = None
    firmware_version: str | None = None

    # Redfish-sourced telemetry; the field-by-field mapping and the
    # core+other error-count sum are in docs/adr/0016's GPU update.
    memory_type: str | None = None
    ecc_mode_enabled: bool | None = None
    correctable_error_count: int | None = None
    uncorrectable_error_count: int | None = None
    temperature_celsius: float | None = None
    power_watts: float | None = None


class Psu(BaseModel):
    """One power supply unit reported for a server."""

    id: str
    model: str | None = None
    serial: str | None = None
    health: str | None = None
    health_detail: str | None = None
    capacity_watts: int | None = None


class Power(BaseModel):
    """A server's reported power supplies."""

    psus: list[Psu] = Field(default_factory=list)


class Hardware(BaseModel):
    """A server's complete vendor-neutral hardware inventory."""

    cpu: Cpu = Field(default_factory=Cpu)
    memory: Memory = Field(default_factory=Memory)
    storage: Storage = Field(default_factory=Storage)
    gpus: list[Gpu] = Field(default_factory=list)
    power: Power = Field(default_factory=Power)
