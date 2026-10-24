export const RACK_W = 0.6;
export const RACK_D = 1.0;
export const COLD = 2.8;
export const HOT = 2.4;
export const Z_MIN = -3.2;

/** Rows come in pairs: fronts face each other across a cold aisle, rears across a hot one. */
export const rowZ = (r: number) => Math.floor(r / 2) * (COLD + HOT) + (r % 2) * COLD;
export const frontSign = (r: number) => (r % 2 === 0 ? 1 : -1);
export const rowYaw = (r: number) => (r % 2 === 0 ? Math.PI : 0);
export const rackX = (rack: number, per: number) => (rack - per / 2) * RACK_W;
export const halfWidth = (per: number) => ((per + 1) * RACK_W) / 2 + 3;
export const zMaxOf = (rows: number) => rowZ(rows - 1) + 3.2;

/** World x of a row-local x (even rows are turned half a circle). */
export const worldX = (row: number, lx: number) => (row % 2 === 0 ? -lx : lx);

export interface Aisle {
  z: number;
  width: number;
  kind: "cold" | "hot";
}

export function aislesOf(rows: number): Aisle[] {
  const out = new Map<string, Aisle>();
  for (let r = 0; r < rows; r++) {
    const s = frontSign(r);
    const front = r + s >= 0 && r + s < rows ? (rowZ(r) + rowZ(r + s)) / 2 : rowZ(r) + s * (RACK_D / 2 + 0.9);
    const rear = r - s >= 0 && r - s < rows ? (rowZ(r) + rowZ(r - s)) / 2 : rowZ(r) - s * (RACK_D / 2 + 0.7);
    out.set(`c${front}`, { z: front, width: COLD - RACK_D, kind: "cold" });
    out.set(`h${rear}`, { z: rear, width: HOT - RACK_D, kind: "hot" });
  }
  return [...out.values()];
}

export interface Pose {
  x: number;
  z: number;
  /** Looking direction on the floor plane. */
  dx: number;
  dz: number;
}

export interface GoTo {
  nonce: number;
  key?: string;
  x?: number;
  z?: number;
}
