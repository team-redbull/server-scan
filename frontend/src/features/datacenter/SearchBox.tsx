import { useMemo, useState } from "react";
import type { RefObject } from "react";

import type { SceneData } from "@/features/datacenter/model";
import { chassisKey, fiKey } from "@/features/datacenter/model";

interface Hit {
  key: string;
  label: string;
  sub: string;
}

/** Find a server, chassis or FI by name or model and fly to it. */
export function SearchBox({
  data,
  inputRef,
  onPick,
}: {
  data: SceneData;
  inputRef: RefObject<HTMLInputElement | null>;
  onPick: (key: string) => void;
}) {
  const [q, setQ] = useState("");
  const hits = useMemo<Hit[]>(() => {
    const needle = q.trim().toLowerCase();
    if (needle.length < 2) return [];
    const out: Hit[] = [];
    data.fis.forEach((f, i) => {
      if (f.name.toLowerCase().includes(needle) || f.serial.toLowerCase().includes(needle)) {
        out.push({ key: fiKey(i), label: f.name, sub: `FI · ${f.serial}` });
      }
    });
    data.chassis.forEach((c, i) => {
      if (c.name.toLowerCase().includes(needle)) out.push({ key: chassisKey(i), label: c.name, sub: `chassis · ${c.model}` });
    });
    for (const u of data.units) {
      if (out.length >= 8) break;
      if (u.name.toLowerCase().includes(needle) || u.model.toLowerCase().includes(needle)) {
        out.push({ key: u.id, label: u.name, sub: `${u.model} · row ${u.row + 1} rack ${u.rack + 1}` });
      }
    }
    return out.slice(0, 8);
  }, [data, q]);

  const pick = (key: string) => {
    onPick(key);
    inputRef.current?.blur();
  };

  return (
    <div data-ui-card className="w-[230px] text-sm">
      <input
        ref={inputRef}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          const first = hits[0];
          if (e.key === "Enter" && first) pick(first.key);
          if (e.key === "Escape") inputRef.current?.blur();
        }}
        placeholder='Search name or model  ( / )'
        aria-label="Search the datacenter"
        className="w-full rounded-md border border-white/25 bg-black/70 px-3 py-1.5 text-zinc-100 placeholder:text-zinc-500 focus:border-sky-400 focus:outline-none"
      />
      {hits.length > 0 && (
        <ul className="mt-1 max-h-72 overflow-y-auto rounded-md border border-white/20 bg-black/85 backdrop-blur">
          {hits.map((h) => (
            <li key={h.key}>
              <button
                type="button"
                onClick={() => pick(h.key)}
                className="block w-full px-3 py-1.5 text-left hover:bg-white/10"
              >
                <span className="block truncate text-zinc-100">{h.label}</span>
                <span className="block truncate text-xs text-zinc-400">{h.sub}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
