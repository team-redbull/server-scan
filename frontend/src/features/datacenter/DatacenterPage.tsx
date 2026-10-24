import { useQueries } from "@tanstack/react-query";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { queryKeys } from "@/api/queryKeys";
import { getServer } from "@/api/servers";
import { StateDot } from "@/features/datacenter/CardShell";
import { Elevation } from "@/features/datacenter/Elevation";
import { ChassisCard, FiCard } from "@/features/datacenter/InfraCards";
import { buildLayout } from "@/features/datacenter/layout";
import { Minimap } from "@/features/datacenter/Minimap";
import {
  fiConnections,
  fromReal,
  generateDemo,
  rackOfKey,
  resolveKey,
} from "@/features/datacenter/model";
import { SearchBox } from "@/features/datacenter/SearchBox";
import { ServerCard } from "@/features/datacenter/ServerCard";
import type { GoTo, Pose } from "@/features/datacenter/world";
import { useServerRowsQuery } from "@/features/inventory/hooks";
import { useSitesQuery } from "@/features/sites/hooks";
import type { ConnectivityAttachment } from "@/types/server";

const DatacenterScene = lazy(() =>
  import("@/features/datacenter/Scene").then((m) => ({ default: m.DatacenterScene })),
);

const NO_ATTACHMENTS: ConnectivityAttachment[] = [];

function hash(text: string) {
  let h = 2166136261;
  for (const ch of text) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return h >>> 0;
}

/** Each demo site is its own deterministic fleet, 300 to 1000 servers. */
function demoFor(site: string) {
  const h = hash(site);
  return generateDemo(300 + (h % 8) * 100, h);
}

/** Walkable 3D proof of concept: one datacenter per site, synthetic or the real seeded fleet. */
export function DatacenterPage() {
  const [params, setParams] = useSearchParams();
  const demo = params.get("mode") !== "real";
  const sitesQuery = useSitesQuery();
  const sites = useMemo(() => sitesQuery.data?.items ?? [], [sitesQuery.data]);
  const site = params.get("site") ?? sites[0]?.site_id ?? null;
  const rows = useServerRowsQuery();
  const layout = useMemo(() => buildLayout(rows.data?.items ?? [], site ?? undefined), [rows.data, site]);

  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [beacon, setBeacon] = useState<string | null>(null);
  const [goto, setGoto] = useState<GoTo | null>(null);
  const poseRef = useRef<Pose>({ x: 0, z: 0, dx: 1, dz: 0 });
  const searchRef = useRef<HTMLInputElement>(null);

  const real = useQueries({
    queries: layout.placed.map((p) => ({
      queryKey: queryKeys.servers.detail(p.row.id),
      queryFn: () => getServer(p.row.id),
      refetchInterval: 30_000,
      enabled: !demo,
    })),
    combine: (results) => ({
      attachments: results.map((r) => r.data?.connectivity.attachments ?? NO_ATTACHMENTS),
      pending: results.some((r) => r.isPending),
    }),
  });

  const demoData = useMemo(() => (site ? demoFor(site) : null), [site]);
  const data = useMemo(() => {
    if (demo) return demoData;
    const links: Record<string, ConnectivityAttachment[]> = {};
    layout.placed.forEach((p, i) => {
      links[p.row.id] = real.attachments[i] ?? NO_ATTACHMENTS;
    });
    return fromReal(layout.placed, layout.racks, links);
  }, [demo, demoData, layout, real.attachments]);

  const resetView = useCallback(() => {
    setHovered(null);
    setSelected(null);
    setBeacon(null);
    setGoto(null);
  }, []);
  const update = useCallback(
    (change: Record<string, string | null>) => {
      const next = new URLSearchParams(params);
      for (const [k, v] of Object.entries(change)) {
        if (v === null) next.delete(k);
        else next.set(k, v);
      }
      setParams(next);
      resetView();
    },
    [params, setParams, resetView],
  );

  const onOpen = useCallback((key: string) => {
    document.exitPointerLock();
    setSelected(key);
  }, []);
  const onClose = useCallback(() => setSelected(null), []);
  const flyTo = useCallback((key: string) => {
    setBeacon(key);
    setGoto((g) => ({ nonce: (g?.nonce ?? 0) + 1, key }));
  }, []);
  const teleport = useCallback((x: number, z: number) => {
    setGoto((g) => ({ nonce: (g?.nonce ?? 0) + 1, x, z }));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || (e.target instanceof Element && e.target.closest("input, textarea"))) return;
      e.preventDefault();
      document.exitPointerLock();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const focus = selected ?? hovered ?? beacon;
  const loading = !data || (!demo && (rows.isPending || real.pending));
  const hoveredThing = data ? resolveKey(data, hovered) : null;
  const picked = data ? resolveKey(data, selected) : null;
  const focusRack = data ? rackOfKey(data, focus) : null;
  const modelCount = data ? new Set(data.units.map((u) => u.model)).size : 0;
  const siteName = sites.find((s) => s.site_id === site)?.name ?? site ?? "";

  return (
    <main className="relative h-full min-h-[320px] bg-black">
      {loading || !data ? (
        <p className="p-6 text-[var(--text-secondary)]">Loading servers…</p>
      ) : data.units.length === 0 ? (
        <p className="p-6 text-[var(--text-secondary)]">No Cisco C-series servers at {siteName}.</p>
      ) : (
        <Suspense fallback={<p className="p-6 text-[var(--text-secondary)]">Loading 3D…</p>}>
          <DatacenterScene
            key={`${demo ? "demo" : "real"}-${site}`}
            data={data}
            focus={focus}
            beacon={beacon}
            goto={goto}
            poseRef={poseRef}
            onHover={setHovered}
            onOpen={onOpen}
          />
        </Suspense>
      )}

      <div className="absolute left-4 top-4 max-w-xs rounded-lg bg-black/60 p-3 text-sm text-zinc-200 backdrop-blur">
        <p className="font-semibold">
          {siteName} · {demo ? "demo fleet" : "real data"}
          {data && ` · ${data.units.length} servers · ${data.rows} rows · ${modelCount} models`}
        </p>
        <div className="mt-2 flex gap-2 text-xs">
          {[
            { mode: "demo", label: "Demo" },
            { mode: "real", label: "Real data" },
          ].map(({ mode, label }) => (
            <button
              key={mode}
              type="button"
              onClick={() => update({ mode: mode === "demo" ? null : mode })}
              className={`rounded-full border px-3 py-1 ${
                (mode === "demo") === demo
                  ? "border-sky-400 bg-sky-500/30 text-white"
                  : "border-zinc-600 text-zinc-300 hover:border-zinc-400"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <p className="pointer-events-none mt-2 text-zinc-400">
          Click to walk · WASD move · Shift run · Ctrl toggles crouch · mouse look · click a server, chassis or FI
          for its card · / search · Esc releases the mouse (then you can use the map)
        </p>
        <p className="pointer-events-none mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
          <span><i className="mr-1 inline-block size-2 rounded-full" style={{ background: "#38a0ff" }} />cold aisle (fronts)</span>
          <span><i className="mr-1 inline-block size-2 rounded-full" style={{ background: "#ff6a2a" }} />hot aisle (rears, cables)</span>
          <span><StateDot state="UP" /> link up</span>
          <span><StateDot state="DOWN" /> link down</span>
          <span><StateDot state="UNKNOWN" /> unknown</span>
          <span>server tint = health</span>
        </p>
      </div>

      <div className="absolute right-4 top-4 flex w-[230px] flex-col items-end gap-2">
        <nav data-ui-card aria-label="Sites" className="flex flex-wrap justify-end gap-1.5">
          {sites.map((s) => (
            <button
              key={s.site_id}
              type="button"
              onClick={() => update({ site: s.site_id })}
              aria-current={s.site_id === site ? "true" : undefined}
              title={`${s.name} (${s.total} servers)`}
              className={`rounded-full border px-3 py-1 text-xs backdrop-blur ${
                s.site_id === site
                  ? "border-sky-400 bg-sky-500/40 text-white"
                  : "border-white/25 bg-black/60 text-zinc-300 hover:border-white/60"
              }`}
            >
              {s.name}
            </button>
          ))}
        </nav>
        {data && (
          <>
            <SearchBox data={data} inputRef={searchRef} onPick={flyTo} />
            <Minimap data={data} poseRef={poseRef} focusRack={focusRack} onGo={teleport} />
          </>
        )}
      </div>

      {data && focusRack && (
        <div className="absolute bottom-6 right-4">
          <Elevation data={data} rack={focusRack} focusKey={focus} onPick={onOpen} />
        </div>
      )}

      <div className="pointer-events-none absolute left-1/2 top-1/2 size-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white/80 ring-1 ring-black/60" />

      {data && picked?.kind === "unit" && <ServerCard unit={picked.unit} onClose={onClose} />}
      {data && picked?.kind === "chassis" && (
        <ChassisCard data={data} index={picked.index} onClose={onClose} onSelect={onOpen} />
      )}
      {data && picked?.kind === "fi" && <FiCard data={data} index={picked.index} onClose={onClose} onSelect={onOpen} />}

      {data && hoveredThing && !picked && (
        <div className="pointer-events-none absolute bottom-6 left-1/2 w-80 -translate-x-1/2 rounded-lg bg-black/75 p-3 text-sm text-zinc-100 backdrop-blur">
          {hoveredThing.kind === "unit" && (
            <>
              <p className="truncate font-semibold">{hoveredThing.unit.name}</p>
              <p className="text-zinc-400">
                Cisco {hoveredThing.unit.model} · row {hoveredThing.unit.row + 1} rack {hoveredThing.unit.rack + 1} ·{" "}
                {hoveredThing.unit.health}
              </p>
              {hoveredThing.unit.links.length > 0 ? (
                <ul className="mt-2 space-y-0.5 text-xs">
                  {hoveredThing.unit.links.map((l, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <StateDot state={l.state} />
                      FI-{l.fabric === 0 ? "A" : "B"} port {l.port} · {l.state}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-xs text-zinc-400">Blade: uplinks belong to its chassis.</p>
              )}
            </>
          )}
          {hoveredThing.kind === "chassis" && (
            <>
              <p className="truncate font-semibold">{hoveredThing.chassis.name}</p>
              <p className="text-zinc-400">
                Cisco {hoveredThing.chassis.model} · {data.units.filter((u) => u.chassis === hoveredThing.index).length} blades
              </p>
              <p className="mt-1 text-xs text-zinc-400">Click for its blades and uplinks.</p>
            </>
          )}
          {hoveredThing.kind === "fi" && (
            <>
              <p className="truncate font-semibold">{hoveredThing.fi.name}</p>
              <p className="text-zinc-400">
                Cisco {hoveredThing.fi.model} · {hoveredThing.fi.serial}
              </p>
              <p className="mt-1 text-xs text-zinc-400">
                {fiConnections(data, hoveredThing.index).length} cables · click for every connected server
              </p>
            </>
          )}
        </div>
      )}
    </main>
  );
}
