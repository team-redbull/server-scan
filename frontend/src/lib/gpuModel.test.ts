import { describe, expect, it } from "vitest";

import { inferredGpuModel } from "@/lib/gpuModel";
import type { GpuInfo } from "@/types/server";

function gpu(model: string | null): GpuInfo {
  return {
    vendor: "NVIDIA",
    model,
    serial: null,
    memory_bytes: null,
    health: null,
    health_detail: null,
    pci_address: null,
    firmware_version: null,
    memory_type: null,
    ecc_mode_enabled: null,
    correctable_error_count: null,
    uncorrectable_error_count: null,
    temperature_celsius: null,
    power_watts: null,
  };
}

describe("inferredGpuModel", () => {
  it("strips the vendor prefix and capacity suffix", () => {
    expect(inferredGpuModel([gpu("NVIDIA A100 80GB")])).toBe("A100");
  });

  it("keeps a multi-word suffix that isn't a capacity", () => {
    expect(inferredGpuModel([gpu("NVIDIA H100 NVL 94GB")])).toBe("H100 NVL");
  });

  it("dedupes identical GPUs across a homogeneous fleet server", () => {
    expect(inferredGpuModel([gpu("NVIDIA A100 80GB"), gpu("NVIDIA A100 80GB")])).toBe("A100");
  });

  it("joins distinct models rather than picking one", () => {
    expect(inferredGpuModel([gpu("NVIDIA A100 80GB"), gpu("NVIDIA H100 80GB")])).toBe(
      "A100, H100",
    );
  });

  it("is null with no GPUs or none carrying a known model", () => {
    expect(inferredGpuModel([])).toBeNull();
    expect(inferredGpuModel([gpu(null)])).toBeNull();
  });
});
