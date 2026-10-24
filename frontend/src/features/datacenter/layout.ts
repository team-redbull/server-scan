import type { ConnectivityAttachment, ServerRow } from "@/types/server";

/** One rack unit in metres (EIA-310). */
export const U = 0.04445;
export const RACK_U = 42;
const USABLE_U = 40;
const MAX_RACKS = 2;

export interface PlacedServer {
  row: ServerRow;
  rack: number;
  u: number;
  h: number;
}

export interface PocLayout {
  placed: PlacedServer[];
  racks: number;
  siteId: string | null;
}

/** C240 is 2U and C220 is 1U; blades are skipped in the POC (they need a chassis). */
export function uHeight(model: string | null): number | null {
  if (!model) return null;
  if (/C2[24]0|C225|C245/i.test(model)) return /C24/i.test(model) ? 2 : 1;
  return null;
}

/** One site's rack servers (the biggest site when none is named), filled into at most two racks bottom-up. */
export function buildLayout(rows: ServerRow[], site?: string): PocLayout {
  const inSite = (r: ServerRow) => site === undefined || (site === "unassigned" ? r.site_id === null : r.site_id === site);
  const candidates = rows.filter((r) => r.vendor === "cisco" && uHeight(r.model) !== null && inSite(r));
  const bySite = new Map<string | null, ServerRow[]>();
  for (const r of candidates) {
    bySite.set(r.site_id, [...(bySite.get(r.site_id) ?? []), r]);
  }
  let siteId: string | null = site ?? null;
  let best: ServerRow[] = [];
  for (const [id, list] of bySite) {
    if (list.length > best.length) {
      siteId = id;
      best = list;
    }
  }

  const placed: PlacedServer[] = [];
  const used = Array<number>(MAX_RACKS).fill(0);
  for (const row of [...best].sort((a, b) => a.name.localeCompare(b.name))) {
    const h = uHeight(row.model) ?? 1;
    const rack = used.findIndex((u) => u + h <= USABLE_U);
    if (rack === -1) break;
    const u = used[rack] ?? 0;
    placed.push({ row, rack, u, h });
    used[rack] = u + h;
  }
  return { placed, racks: used.filter((u) => u > 0).length, siteId };
}

/** Physical cabled uplinks only; vNICs ride on them and are not cables. */
export function physicalLinks(attachments: ConnectivityAttachment[]): ConnectivityAttachment[] {
  return attachments.filter((a) => a.interface_kind === "PHYSICAL");
}

/** Each distinct fabric interconnect name gets one 1U slot, in sorted order. */
export function fabricSlots(all: ConnectivityAttachment[][]): string[] {
  const names = new Set<string>();
  for (const list of all) {
    for (const a of physicalLinks(list)) {
      if (a.fabric_name) names.add(a.fabric_name);
    }
  }
  return [...names].sort();
}
