import * as THREE from "three";

import { U } from "@/features/datacenter/layout";
import { MODELS } from "@/features/datacenter/model";

const PX_PER_M = 2000;
const cache = new Map<string, THREE.CanvasTexture>();

function canvasTexture(w: number, h: number, draw: (c: CanvasRenderingContext2D) => void) {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (ctx) draw(ctx);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  return tex;
}

function cached(key: string, make: () => THREE.CanvasTexture) {
  let tex = cache.get(key);
  if (!tex) {
    tex = make();
    cache.set(key, tex);
  }
  return tex;
}

function ears(c: CanvasRenderingContext2D, w: number, h: number) {
  c.fillStyle = "#b9bcc2";
  c.fillRect(0, 0, w * 0.035, h);
  c.fillRect(w * 0.965, 0, w * 0.035, h);
}

/** One front-panel texture per model: drive bays, LEDs and the model label. */
export function frontTexture(model: string) {
  return cached(`front:${model}`, () => {
    const spec = MODELS[model] ?? MODELS["C220 M6"];
    if (!spec) throw new Error("model catalog is empty");
    const W = Math.round(spec.w * PX_PER_M);
    const H = Math.round(spec.h * U * PX_PER_M);
    return canvasTexture(W, H, (c) => {
      c.fillStyle = spec.kind === "fi" ? "#1c3b4d" : spec.kind === "chassis" ? "#1d2024" : "#2b2e34";
      c.fillRect(0, 0, W, H);
      const font = Math.max(14, Math.min(36, H * 0.34));
      c.fillStyle = "#cfd3da";
      c.font = `bold ${font}px sans-serif`;
      if (spec.kind === "rack" || spec.kind === "blade") {
        ears(c, W, H);
        const rows = spec.kind === "blade" ? 1 : spec.h >= 4 ? 4 : spec.h >= 2 ? 2 : 1;
        const cols = Math.ceil(spec.bays / rows);
        const area = spec.kind === "blade" ? W * 0.5 : W * 0.62;
        const bw = area / cols - 5;
        const bh = (H * 0.8) / rows - 5;
        for (let r = 0; r < rows; r++) {
          for (let k = 0; k < cols; k++) {
            c.fillStyle = "#15171a";
            c.fillRect(W * 0.05 + k * (bw + 5), H * 0.1 + r * (bh + 5), bw, bh);
            c.fillStyle = "#4a4e57";
            c.fillRect(W * 0.05 + k * (bw + 5) + 2, H * 0.1 + r * (bh + 5) + 2, bw - 4, Math.max(2, bh * 0.12));
          }
        }
        const lx = spec.kind === "blade" ? W * 0.62 : W * 0.7;
        c.fillStyle = "#049fd9";
        c.fillRect(lx, H * 0.12, font * 0.4, font * 0.4);
        c.fillStyle = "#22d37a";
        c.fillRect(lx + font * 0.6, H * 0.12, font * 0.4, font * 0.4);
        c.fillStyle = "#cfd3da";
        c.font = `bold ${spec.kind === "blade" ? font * 0.75 : font}px sans-serif`;
        c.fillText(model, lx, H * 0.78);
      } else if (spec.kind === "chassis") {
        c.strokeStyle = "#4a4e57";
        c.lineWidth = 3;
        c.strokeRect(6, 6, W - 12, H - 12);
        c.fillText(`UCS ${model.replace("UCS ", "")}`, W * 0.04, H * 0.1 + font);
      } else {
        ears(c, W, H);
        c.fillStyle = "#7fd0ff";
        c.fillText(model, W * 0.06, H * 0.62);
      }
    });
  });
}

/** One rear texture per model: PSUs, IOMs or the FI port grid. */
export function rearTexture(model: string) {
  return cached(`rear:${model}`, () => {
    const spec = MODELS[model] ?? MODELS["C220 M6"];
    if (!spec) throw new Error("model catalog is empty");
    const W = Math.round(spec.w * PX_PER_M);
    const H = Math.round(spec.h * U * PX_PER_M);
    return canvasTexture(W, H, (c) => {
      c.fillStyle = "#14161a";
      c.fillRect(0, 0, W, H);
      ears(c, W, H);
      if (spec.kind === "fi") {
        for (let r = 0; r < 2; r++) {
          for (let k = 0; k < 16; k++) {
            c.fillStyle = "#050607";
            c.fillRect(W * 0.06 + k * (W * 0.056), H * 0.12 + r * (H * 0.42), W * 0.04, H * 0.34);
            c.fillStyle = "#d9b441";
            c.fillRect(W * 0.065 + k * (W * 0.056), H * 0.2 + r * (H * 0.42), W * 0.03, H * 0.16);
          }
        }
        return;
      }
      const psus = spec.kind === "chassis" ? 4 : 2;
      for (let p = 0; p < psus; p++) {
        c.fillStyle = "#2c2f35";
        c.fillRect(W * 0.06 + p * W * 0.2, H * 0.08, W * 0.18, H * 0.84);
        c.fillStyle = "#1a1c20";
        c.fillRect(W * 0.07 + p * W * 0.2, H * 0.14, W * 0.16, H * 0.72);
        c.fillStyle = "#22d37a";
        c.fillRect(W * 0.07 + p * W * 0.2, H * 0.86, W * 0.03, H * 0.04);
      }
      if (spec.kind === "rack") {
        for (let k = 0; k < 4; k++) {
          c.fillStyle = "#050607";
          c.fillRect(W * 0.5 + k * W * 0.1, H * 0.3, W * 0.07, H * 0.4);
          c.fillStyle = "#d9b441";
          c.fillRect(W * 0.51 + k * W * 0.1, H * 0.42, W * 0.05, H * 0.16);
        }
      } else {
        c.fillStyle = "#1e3b4d";
        c.fillRect(W * 0.1 + W * 0.8 * 0.5, H * 0.15, W * 0.35, H * 0.7);
      }
    });
  });
}

export function labelTexture(text: string) {
  return cached(`label:${text}`, () =>
    canvasTexture(512, 128, (c) => {
      c.fillStyle = "#0b3c5d";
      c.fillRect(0, 0, 512, 128);
      c.fillStyle = "#e6edf3";
      c.font = "bold 44px sans-serif";
      c.textAlign = "center";
      c.fillText(text, 256, 78);
    }),
  );
}

export function floorTexture() {
  return cached("floor", () => {
    const tex = canvasTexture(256, 256, (c) => {
      c.fillStyle = "#6e7378";
      c.fillRect(0, 0, 256, 256);
      c.strokeStyle = "#4b4f54";
      c.lineWidth = 6;
      c.strokeRect(0, 0, 256, 256);
      c.fillStyle = "#7a7f85";
      for (let x = 24; x < 256; x += 32) {
        for (let y = 24; y < 256; y += 32) c.fillRect(x, y, 4, 4);
      }
    });
    tex.wrapS = THREE.RepeatWrapping;
    tex.wrapT = THREE.RepeatWrapping;
    return tex;
  });
}

/** Translucent floor band with a repeating label; the blue cold aisle is in front of servers, the orange hot aisle behind. */
export function aisleTexture(kind: "cold" | "hot") {
  return cached(`aisle:${kind}`, () => {
    const cold = kind === "cold";
    const tex = canvasTexture(512, 128, (c) => {
      c.fillStyle = cold ? "rgba(40,140,255,0.42)" : "rgba(255,90,30,0.42)";
      c.fillRect(0, 0, 512, 128);
      c.fillStyle = "rgba(255,255,255,0.9)";
      c.font = "bold 40px sans-serif";
      c.textAlign = "center";
      c.fillText(cold ? "COLD AISLE  ›››" : "HOT AISLE  ›››", 256, 78);
      c.fillStyle = cold ? "rgba(120,190,255,0.9)" : "rgba(255,160,110,0.9)";
      c.fillRect(0, 0, 512, 6);
      c.fillRect(0, 122, 512, 6);
    });
    tex.wrapS = THREE.RepeatWrapping;
    return tex;
  });
}
