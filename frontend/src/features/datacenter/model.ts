import type { PlacedServer } from "@/features/datacenter/layout";
import { fabricSlots, physicalLinks } from "@/features/datacenter/layout";
import type { ConnectivityAttachment, HealthSeverity } from "@/types/server";

export type ModelKind = "rack" | "blade" | "chassis" | "fi";

export interface ModelSpec {
  kind: ModelKind;
  /** Height in rack units (blades: their slot height inside the chassis). */
  h: number;
  /** Front width in metres. */
  w: number;
  bays: number;
}

const FULL = 0.46;
const HALF = 0.225;

export const MODELS: Record<string, ModelSpec> = {
  "C220 M5": { kind: "rack", h: 1, w: FULL, bays: 10 },
  "C220 M6": { kind: "rack", h: 1, w: FULL, bays: 10 },
  "C220 M7": { kind: "rack", h: 1, w: FULL, bays: 10 },
  "C225 M6": { kind: "rack", h: 1, w: FULL, bays: 10 },
  "C240 M5": { kind: "rack", h: 2, w: FULL, bays: 24 },
  "C240 M6": { kind: "rack", h: 2, w: FULL, bays: 24 },
  "C240 M7": { kind: "rack", h: 2, w: FULL, bays: 24 },
  "C245 M6": { kind: "rack", h: 2, w: FULL, bays: 24 },
  "C480 M5": { kind: "rack", h: 4, w: FULL, bays: 24 },
  "S3260 M5": { kind: "rack", h: 4, w: FULL, bays: 28 },
  "B200 M5": { kind: "blade", h: 1.5, w: HALF, bays: 2 },
  "B200 M6": { kind: "blade", h: 1.5, w: HALF, bays: 2 },
  "X210c M6": { kind: "blade", h: 1.75, w: HALF, bays: 2 },
  "X210c M7": { kind: "blade", h: 1.75, w: HALF, bays: 2 },
  "UCS 5108": { kind: "chassis", h: 6, w: FULL, bays: 0 },
  "UCS X9508": { kind: "chassis", h: 7, w: FULL, bays: 0 },
  "FI 6454": { kind: "fi", h: 1, w: FULL, bays: 0 },
};

const RACK_MODELS = Object.keys(MODELS).filter((m) => MODELS[m]?.kind === "rack");
const BLADE_SETS: { chassis: string; blades: string[] }[] = [
  { chassis: "UCS 5108", blades: ["B200 M5", "B200 M6"] },
  { chassis: "UCS X9508", blades: ["X210c M6", "X210c M7"] },
];

export interface Link {
  /** Index of the row's FI (0 = A, 1 = B). */
  fabric: number;
  state: string;
  /** FI-side port label, e.g. "1/7". */
  port: string;
  speedMbps: number | null;
}

export interface Unit {
  id: string;
  name: string;
  model: string;
  row: number;
  rack: number;
  /** Bottom edge in rack units (fractional for blades). */
  u: number;
  dx: number;
  health: HealthSeverity;
  links: Link[];
  /** Index into `SceneData.chassis` for a blade. */
  chassis?: number;
}

export interface Chassis {
  name: string;
  model: string;
  row: number;
  rack: number;
  u: number;
  links: Link[];
}

export interface Fi {
  row: number;
  slot: number;
  name: string;
  model: string;
  serial: string;
  ports: number;
}

export interface SceneData {
  rows: number;
  /** Server racks per row; the FI rack is the extra one at this index. */
  racksPerRow: number;
  racks: { row: number; rack: number }[];
  units: Unit[];
  chassis: Chassis[];
  fis: Fi[];
}

export function modelKey(model: string | null): string {
  const key = (model ?? "").replace(/^UCS\s+/i, "");
  return MODELS[key] ? key : "C220 M6";
}

function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick<T>(rnd: () => number, list: T[]): T {
  return list[Math.floor(rnd() * list.length)] as T;
}

function health(rnd: () => number): HealthSeverity {
  const r = rnd();
  if (r < 0.7) return "HEALTHY";
  if (r < 0.82) return "WARNING";
  if (r < 0.87) return "MAJOR";
  if (r < 0.97) return "CRITICAL";
  return "UNKNOWN";
}

function linkState(rnd: () => number): string {
  const r = rnd();
  return r < 0.9 ? "UP" : r < 0.96 ? "DOWN" : "UNKNOWN";
}

function makeLinks(rnd: () => number, n: number, row: number, speedMbps: number, ports: Map<string, number>): Link[] {
  return Array.from({ length: n }, (_, i) => {
    const key = `${row}:${i % 2}`;
    const port = (ports.get(key) ?? 0) + 1;
    ports.set(key, port);
    return { fabric: i % 2, state: linkState(rnd), port: `1/${port}`, speedMbps };
  });
}

/** Deterministic synthetic fleet: random models, racks, rows and chassis. */
export function generateDemo(target = 1000, seed = 7): SceneData {
  const rnd = mulberry32(seed);
  const racksPerRow = 9;
  const usable = 40;
  const units: Unit[] = [];
  const chassis: Chassis[] = [];
  const racks: { row: number; rack: number }[] = [];
  const ports = new Map<string, number>();
  let rackNo = 0;

  while (units.length < target) {
    const row = Math.floor(rackNo / racksPerRow);
    const rack = rackNo % racksPerRow;
    rackNo += 1;
    racks.push({ row, rack });
    const tag = `r${row + 1}k${rack + 1}`;
    let u = 0;
    while (u < usable && units.length < target) {
      if (rnd() < 0.04) {
        u += 1;
        continue;
      }
      if (rnd() < 0.28) {
        const set = pick(rnd, BLADE_SETS);
        const spec = MODELS[set.chassis];
        if (spec && u + spec.h <= usable) {
          const ci =
            chassis.push({
              name: `ucs-${tag}-c${u + 1}`,
              model: set.chassis,
              row,
              rack,
              u,
              links: makeLinks(rnd, 4, row, 40000, ports),
            }) - 1;
          const bladeModel = pick(rnd, set.blades);
          const slotH = (MODELS[bladeModel]?.h ?? 1.5);
          for (let slot = 0; slot < 8 && units.length < target; slot++) {
            units.push({
              id: `demo-${units.length}`,
              name: `ucs-${tag}-c${u + 1}-s${slot + 1}`,
              model: bladeModel,
              row,
              rack,
              u: u + Math.floor(slot / 2) * slotH,
              dx: slot % 2 === 0 ? -0.1175 : 0.1175,
              health: health(rnd),
              links: [],
              chassis: ci,
            });
          }
          u += spec.h;
          continue;
        }
      }
      let model = pick(rnd, RACK_MODELS);
      if (u + (MODELS[model]?.h ?? 1) > usable) model = "C220 M6";
      const h = MODELS[model]?.h ?? 1;
      if (u + h > usable) break;
      units.push({
        id: `demo-${units.length}`,
        name: `ucs-${tag}-u${u + 1}`,
        model,
        row,
        rack,
        u,
        dx: 0,
        health: health(rnd),
        links: makeLinks(rnd, h >= 2 ? 4 : 2, row, 25000, ports),
      });
      u += h;
    }
  }

  const rows = Math.ceil(racks.length / racksPerRow);
  const fis: Fi[] = Array.from({ length: rows }, (_, row) =>
    [0, 1].map((slot) => ({
      row,
      slot,
      name: `FI-${slot === 0 ? "A" : "B"}-row${row + 1}`,
      model: "UCS-FI-6454",
      serial: `FDO2${row + 1}${slot === 0 ? "A" : "B"}${Math.floor(rnd() * 0xfffff).toString(16).toUpperCase().padStart(5, "0")}`,
      ports: 54,
    })),
  ).flat();
  return { rows, racksPerRow, racks, units, chassis, fis };
}

/** The real seeded servers in the same shape, one row, links read from the API. */
export function fromReal(
  placed: PlacedServer[],
  racks: number,
  links: Record<string, ConnectivityAttachment[]>,
): SceneData {
  const fabrics = fabricSlots(Object.values(links));
  const attachments = Object.values(links).flat();
  const units: Unit[] = placed.map((p) => ({
    id: p.row.id,
    name: p.row.name,
    model: modelKey(p.row.model),
    row: 0,
    rack: p.rack,
    u: p.u,
    dx: 0,
    health: p.row.health,
    links: physicalLinks(links[p.row.id] ?? []).flatMap((a) => {
      const fabric = a.fabric_name ? fabrics.indexOf(a.fabric_name) : -1;
      return fabric < 0
        ? []
        : [{ fabric, state: a.oper_state, port: a.fabric_port ?? "?", speedMbps: a.speed_mbps }];
    }),
  }));
  return {
    rows: 1,
    racksPerRow: racks,
    racks: Array.from({ length: racks }, (_, rack) => ({ row: 0, rack })),
    units,
    chassis: [],
    fis: fabrics.map((name, slot) => {
      const a = attachments.find((x) => x.fabric_name === name);
      return { row: 0, slot, name, model: a?.fabric_model ?? "UCS FI", serial: a?.fabric_serial ?? "unknown", ports: 54 };
    }),
  };
}

export const chassisKey = (i: number) => `chassis:${i}`;
export const fiKey = (i: number) => `fi:${i}`;

export type Resolved =
  | { kind: "unit"; unit: Unit }
  | { kind: "chassis"; index: number; chassis: Chassis }
  | { kind: "fi"; index: number; fi: Fi };

/** Turn a pick key (a server id, "chassis:N" or "fi:N") into the thing it names. */
export function resolveKey(data: SceneData, key: string | null): Resolved | null {
  if (!key) return null;
  if (key.startsWith("chassis:")) {
    const index = Number(key.slice(8));
    const chassis = data.chassis[index];
    return chassis ? { kind: "chassis", index, chassis } : null;
  }
  if (key.startsWith("fi:")) {
    const index = Number(key.slice(3));
    const fi = data.fis[index];
    return fi ? { kind: "fi", index, fi } : null;
  }
  const unit = data.units.find((u) => u.id === key);
  return unit ? { kind: "unit", unit } : null;
}

/** The rack (row, index) a picked thing stands in; the FI rack is index `racksPerRow`. */
export function rackOfKey(data: SceneData, key: string | null): { row: number; rack: number } | null {
  const r = resolveKey(data, key);
  if (!r) return null;
  if (r.kind === "unit") return { row: r.unit.row, rack: r.unit.rack };
  if (r.kind === "chassis") return { row: r.chassis.row, rack: r.chassis.rack };
  return { row: r.fi.row, rack: data.racksPerRow };
}

/** The key whose cables a pick owns: a blade's uplinks belong to its chassis. */
export function cableOwnerKey(data: SceneData, key: string | null): string | null {
  const r = resolveKey(data, key);
  if (!r) return null;
  if (r.kind === "unit") return r.unit.chassis === undefined ? r.unit.id : chassisKey(r.unit.chassis);
  return key;
}

export interface Connection {
  key: string;
  name: string;
  model: string;
  port: string;
  state: string;
  speedMbps: number | null;
  blades: string[];
}

function portNumber(port: string) {
  return Number(port.split("/").pop()) || 0;
}

/** Everything cabled to one FI, by port; a chassis lists the blades behind it. */
export function fiConnections(data: SceneData, fiIndex: number): Connection[] {
  const fi = data.fis[fiIndex];
  if (!fi) return [];
  const out: Connection[] = [];
  for (const u of data.units) {
    if (u.row !== fi.row) continue;
    for (const l of u.links) {
      if (l.fabric === fi.slot) {
        out.push({ key: u.id, name: u.name, model: u.model, port: l.port, state: l.state, speedMbps: l.speedMbps, blades: [] });
      }
    }
  }
  data.chassis.forEach((c, i) => {
    if (c.row !== fi.row) return;
    for (const l of c.links) {
      if (l.fabric === fi.slot) {
        const blades = data.units.filter((u) => u.chassis === i).map((u) => u.name);
        out.push({ key: chassisKey(i), name: c.name, model: c.model, port: l.port, state: l.state, speedMbps: l.speedMbps, blades });
      }
    }
  });
  return out.sort((a, b) => portNumber(a.port) - portNumber(b.port));
}

export function formatSpeed(mbps: number | null): string {
  if (mbps === null) return "—";
  return mbps >= 1000 ? `${mbps / 1000}G` : `${mbps}M`;
}
