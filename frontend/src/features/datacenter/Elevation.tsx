import type { SceneData } from "@/features/datacenter/model";
import { chassisKey, fiKey, MODELS } from "@/features/datacenter/model";
import type { HealthSeverity } from "@/types/server";

const PX = 6;
const WIDTH = 170;
const COLOR: Record<HealthSeverity, string> = {
  HEALTHY: "#2f9e6a",
  WARNING: "#c9a227",
  MAJOR: "#d9772b",
  CRITICAL: "#d63b3b",
  UNKNOWN: "#6b7280",
};

interface Block {
  key: string;
  label: string;
  u: number;
  h: number;
  color: string;
  subs: Block[];
  dx?: number;
}

function blocksOf(data: SceneData, row: number, rack: number): Block[] {
  const out: Block[] = [];
  if (rack === data.racksPerRow) {
    data.fis.forEach((f, i) => {
      if (f.row === row) out.push({ key: fiKey(i), label: f.name, u: 30 + f.slot, h: 1, color: "#1f6f9c", subs: [] });
    });
    return out;
  }
  for (const u of data.units) {
    const spec = MODELS[u.model];
    if (u.row === row && u.rack === rack && spec && spec.kind !== "blade") {
      out.push({ key: u.id, label: u.model, u: u.u, h: spec.h, color: COLOR[u.health], subs: [] });
    }
  }
  data.chassis.forEach((c, i) => {
    if (c.row !== row || c.rack !== rack) return;
    const subs = data.units
      .filter((b) => b.chassis === i)
      .map((b) => ({ key: b.id, label: b.model, u: b.u, h: MODELS[b.model]?.h ?? 1.5, color: COLOR[b.health], subs: [] as Block[], dx: b.dx }));
    out.push({ key: chassisKey(i), label: c.model, u: c.u, h: MODELS[c.model]?.h ?? 6, color: "#3b3f47", subs });
  });
  return out;
}

/** Flat front view of one 42U rack; the lit block is what you are looking at. */
export function Elevation({
  data,
  rack,
  focusKey,
  onPick,
}: {
  data: SceneData;
  rack: { row: number; rack: number };
  focusKey: string | null;
  onPick: (key: string) => void;
}) {
  const blocks = blocksOf(data, rack.row, rack.rack);
  const top = (b: { u: number; h: number }) => (42 - b.u - b.h) * PX;
  const isFi = rack.rack === data.racksPerRow;
  return (
    <div className="rounded-lg bg-black/70 p-2 text-xs text-zinc-200 backdrop-blur">
      <p className="mb-1 font-semibold">
        Row {rack.row + 1} · {isFi ? "FI rack" : `rack ${rack.rack + 1}`}
      </p>
      <svg width={WIDTH} height={42 * PX + 2} role="img" aria-label="Rack elevation">
        <rect x={0} y={0} width={WIDTH} height={42 * PX + 2} fill="#14161a" stroke="#4a4e57" />
        {Array.from({ length: 9 }, (_, i) => (
          <text key={i} x={WIDTH - 2} y={(42 - i * 5) * PX - 1} fontSize={7} fill="#6b7280" textAnchor="end">
            {i * 5 + 1}
          </text>
        ))}
        {blocks.map((b) => (
          <g key={b.key} onClick={() => onPick(b.key)} className="cursor-pointer">
            <rect
              x={2}
              y={top(b) + 1}
              width={WIDTH - 22}
              height={b.h * PX - 1}
              fill={b.color}
              stroke={focusKey === b.key ? "#ffffff" : "#0b0c0e"}
              strokeWidth={focusKey === b.key ? 2 : 1}
            >
              <title>{b.label}</title>
            </rect>
            {b.subs.length === 0 && b.h * PX >= 9 && (
              <text x={5} y={top(b) + Math.min(b.h * PX - 3, 10)} fontSize={7} fill="#fff">
                {b.label}
              </text>
            )}
            {b.subs.map((s) => (
              <rect
                key={s.key}
                x={2 + ((s.dx ?? 0) < 0 ? 0 : (WIDTH - 22) / 2) + 1}
                y={top(s) + 1}
                width={(WIDTH - 22) / 2 - 3}
                height={s.h * PX - 2}
                fill={s.color}
                stroke={focusKey === s.key ? "#ffffff" : "#0b0c0e"}
                strokeWidth={focusKey === s.key ? 2 : 1}
                onClick={(e) => {
                  e.stopPropagation();
                  onPick(s.key);
                }}
              />
            ))}
          </g>
        ))}
      </svg>
    </div>
  );
}
