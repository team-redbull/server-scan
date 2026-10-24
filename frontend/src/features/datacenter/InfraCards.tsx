import { useMemo } from "react";

import { CardShell, StateDot } from "@/features/datacenter/CardShell";
import type { SceneData } from "@/features/datacenter/model";
import { fiConnections, formatSpeed } from "@/features/datacenter/model";

interface CardProps {
  data: SceneData;
  index: number;
  onClose: () => void;
  onSelect: (key: string) => void;
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-2">
      <dt className="text-xs text-[var(--text-muted)]">{label}</dt>
      <dd className="text-lg font-semibold">{value}</dd>
    </div>
  );
}

/** A fabric interconnect: identity, port usage and every server cabled to it. */
export function FiCard({ data, index, onClose, onSelect }: CardProps) {
  const fi = data.fis[index];
  const conns = useMemo(() => fiConnections(data, index), [data, index]);
  if (!fi) return null;
  const count = (state: string) => conns.filter((c) => c.state === state).length;
  const speeds = [...new Set(conns.map((c) => formatSpeed(c.speedMbps)))].join(", ") || "—";
  return (
    <CardShell label={`Fabric interconnect ${fi.name}`} onClose={onClose}>
      <h2 className="text-2xl font-semibold">{fi.name}</h2>
      <p className="text-sm text-[var(--text-secondary)]">
        Cisco {fi.model} · serial <span className="font-mono">{fi.serial}</span> · row {fi.row + 1}
      </p>
      <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Stat label="Ports used" value={`${conns.length} / ${fi.ports}`} />
        <Stat label="Link speeds" value={speeds} />
        <Stat label="Up" value={count("UP")} />
        <Stat label="Down" value={count("DOWN")} />
        <Stat label="Unknown" value={count("UNKNOWN")} />
      </dl>
      <h3 className="mt-5 text-sm font-semibold">Connected servers ({conns.length} cables)</h3>
      <div className="mt-2 max-h-[48vh] overflow-y-auto rounded-lg border border-[var(--border-subtle)]">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-[var(--surface-raised)] text-left text-xs text-[var(--text-muted)]">
            <tr>
              <th className="px-3 py-2">Port</th>
              <th className="px-3 py-2">Server</th>
              <th className="px-3 py-2">Model</th>
              <th className="px-3 py-2">Speed</th>
              <th className="px-3 py-2">State</th>
            </tr>
          </thead>
          <tbody>
            {conns.map((c, i) => (
              <tr key={`${c.key}${c.port}${i}`} className="border-t border-[var(--border-subtle)] align-top">
                <td className="px-3 py-1.5 font-mono">{c.port}</td>
                <td className="px-3 py-1.5">
                  <button type="button" className="text-left text-sky-400 hover:underline" onClick={() => onSelect(c.key)}>
                    {c.name}
                  </button>
                  {c.blades.length > 0 && (
                    <p className="text-xs text-[var(--text-muted)]">{c.blades.length} blades: {c.blades.join(", ")}</p>
                  )}
                </td>
                <td className="px-3 py-1.5">{c.model}</td>
                <td className="px-3 py-1.5">{formatSpeed(c.speedMbps)}</td>
                <td className="px-3 py-1.5">
                  <span className="inline-flex items-center gap-2">
                    <StateDot state={c.state} />
                    {c.state}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </CardShell>
  );
}

/** A blade chassis: its blades and the FI ports its uplinks use. */
export function ChassisCard({ data, index, onClose, onSelect }: CardProps) {
  const chassis = data.chassis[index];
  if (!chassis) return null;
  const blades = data.units.filter((u) => u.chassis === index);
  return (
    <CardShell label={`Chassis ${chassis.name}`} onClose={onClose}>
      <h2 className="text-2xl font-semibold">{chassis.name}</h2>
      <p className="text-sm text-[var(--text-secondary)]">
        Cisco {chassis.model} · row {chassis.row + 1} rack {chassis.rack + 1} · U{Math.floor(chassis.u) + 1}
      </p>
      <h3 className="mt-4 text-sm font-semibold">Uplinks</h3>
      <ul className="mt-1 space-y-1 text-sm">
        {chassis.links.map((l, i) => (
          <li key={i} className="flex items-center gap-2">
            <StateDot state={l.state} />
            {data.fis.find((f) => f.row === chassis.row && f.slot === l.fabric)?.name ?? "FI"} port {l.port} ·{" "}
            {formatSpeed(l.speedMbps)} · {l.state}
          </li>
        ))}
      </ul>
      <h3 className="mt-4 text-sm font-semibold">Blades ({blades.length})</h3>
      <ul className="mt-1 grid gap-1 text-sm sm:grid-cols-2">
        {blades.map((b) => (
          <li key={b.id}>
            <button type="button" className="text-sky-400 hover:underline" onClick={() => onSelect(b.id)}>
              {b.name}
            </button>{" "}
            <span className="text-[var(--text-muted)]">
              {b.model} · {b.health}
            </span>
          </li>
        ))}
      </ul>
    </CardShell>
  );
}
