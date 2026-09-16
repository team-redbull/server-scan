import type { GpuInfo } from "@/types/server";

/** Strips a `GpuCatalog`-enriched name ("NVIDIA A100 80GB") down to its
 * marketing short name ("A100") — vendor prefix and capacity suffix only. */
const VENDOR_PREFIX = /^(NVIDIA|AMD|Intel)\s+/;
const CAPACITY_SUFFIX = /\s+\d+GB$/;

function shortenGpuModel(model: string): string {
  return model.replace(VENDOR_PREFIX, "").replace(CAPACITY_SUFFIX, "");
}

/**
 * A short, GPU-derived hint for a server whose own Model is unread.
 *
 * Never written back to `Server.model` — display only, so a DGX/HGX host
 * whose BMC omits `ComputerSystem.Model` shows "A100" instead of a bare
 * "—", derived from the already-enriched `GpuInfo.model`.
 */
export function inferredGpuModel(gpus: GpuInfo[]): string | null {
  const names = new Set<string>();
  for (const gpu of gpus) {
    if (gpu.model) {
      names.add(shortenGpuModel(gpu.model));
    }
  }
  return names.size > 0 ? [...names].join(", ") : null;
}
