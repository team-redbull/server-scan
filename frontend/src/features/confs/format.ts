import type { Conf } from "@/api/confs";

export const UNKNOWN = "unknown";

/** `{count, model}` entries summed per model, first-seen order kept, so a
 * model listed twice still shows once. */
export function groupByModel(entries: { count: number; model: string }[]) {
  const totals = new Map<string, number>();
  for (const entry of entries) {
    totals.set(entry.model, (totals.get(entry.model) ?? 0) + entry.count);
  }
  return [...totals].map(([model, count]) => ({ model, count }));
}

/** The GPUs grouped by model; when the writer lists none but counts some, one
 * entry of `gpu_count` with the model `"unknown"`. */
export function gpuEntries(conf: Conf): { count: number; model: string }[] {
  const grouped = groupByModel(conf.gpus);
  if (grouped.length === 0 && conf.gpu_count > 0) return [{ count: conf.gpu_count, model: UNKNOWN }];
  return grouped;
}

/** Whether the conf records a VRAM size; a GPU-less conf stores `null` or `0`. */
export function hasVram(conf: Conf): boolean {
  return conf.gpu_vram_gb !== null && conf.gpu_vram_gb > 0;
}

/** A capacity in TB as the shortest exact text: `1.92 TB`, `8 TB`. */
export function formatTb(tb: number): string {
  return `${String(Number(tb.toFixed(2)))} TB`;
}

/** MHz as GHz with one decimal: `2600` -> `2.6 GHz`. */
export function formatGhz(mhz: number): string {
  return `${(mhz / 1000).toFixed(1)} GHz`;
}

/** A cache size in KB, as MB once it reaches 1 MB. */
export function formatKb(kb: number): string {
  if (kb < 1024) return `${String(kb)} KB`;
  return `${String(Number((kb / 1024).toFixed(1)))} MB`;
}

/** Every drive's capacity added up, in TB. */
export function totalStorageTb(conf: Conf): number {
  return conf.disks.reduce((sum, disk) => sum + disk.count * disk.capacity_tb, 0);
}

/** The text a conf is searched by: ids, component models and disk types. */
export function searchText(conf: Conf): string {
  return [
    conf.conf_id,
    conf.part_number ?? "",
    conf.model,
    conf.cpu.model,
    conf.cpu.vendor ?? "",
    ...conf.ram.type,
    ...conf.disks.map((disk) => disk.type),
    ...conf.gpus.map((gpu) => gpu.model),
  ]
    .join("\n")
    .toLowerCase();
}

/** Confs matching every whitespace-separated term, in the API's natural `conf_id` order. */
export function visibleConfs(items: Conf[], search: string): Conf[] {
  const terms = search.toLowerCase().split(/\s+/).filter(Boolean);
  return items.filter((conf) => {
    const text = searchText(conf);
    return terms.every((term) => text.includes(term));
  });
}

/** One adapter group as text: `2× 2-port 100 Gb/s`. */
export function adapterText(adapter: Conf["adapters"][number]): string {
  return `${String(adapter.count)}× ${String(adapter.ports)}-port ${String(adapter.speed)} Gb/s`;
}
