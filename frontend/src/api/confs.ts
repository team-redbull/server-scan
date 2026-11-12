import { apiFetch } from "@/api/client";

/** One server configuration (spec) from the `confs` collection; the shape is
 * the writer's, reproduced field for field (docs/adr/0061-confs.md). */
export interface Conf {
  conf_id: string;
  /** `null` when the conf has no catalog match (an unmapped conf). */
  part_number: string | null;
  model: string;
  cpu: {
    model: string;
    vendor: string | null;
    sockets: number;
    cores_per_cpu: number;
    total_cores_ht: number;
    clock_mhz: number;
    max_clock_mhz: number;
    cache_kb: { l1: number; l2: number; l3: number };
  };
  ram: {
    /** `0` when the memory scan failed. */
    total_gib: number;
    dimms: number;
    per_dimm_gb: number;
    /** `[]` when the DIMM read failed. */
    type: string[];
  };
  /** No drive model is recorded; `type` (`NVME`, `SSD`, …) may be `"unknown"`. */
  disks: { count: number; type: string; capacity_tb: number }[];
  /** Grouped by model; may be `[]` while `gpu_count` is not 0. */
  gpus: { count: number; model: string }[];
  gpu_count: number;
  /** VRAM per GPU; `null` or `0` on a GPU-less conf (the UI shows neither). */
  gpu_vram_gb: number | null;
  /** No adapter model is recorded; `speed` is per port, in Gb/s. */
  adapters: { count: number; ports: number; speed: number }[];
  adapter_count: number;
}

export interface ConfListResponse {
  items: Conf[];
  /** `conf_id` of each stored document the API skipped as unreadable. */
  unreadable: string[];
}

export function listConfs(): Promise<ConfListResponse> {
  return apiFetch<ConfListResponse>("/api/v1/confs");
}
