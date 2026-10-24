import { CardShell, StateDot } from "@/features/datacenter/CardShell";
import type { Unit } from "@/features/datacenter/model";
import { formatSpeed } from "@/features/datacenter/model";
import { ServerDetailView } from "@/features/servers/ServerDetailPage";

function DemoDetails({ unit }: { unit: Unit }) {
  return (
    <div>
      <h2 className="text-2xl font-semibold">{unit.name}</h2>
      <p className="text-sm text-[var(--text-secondary)]">Cisco {unit.model} · synthetic demo server</p>
      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
        <dt className="text-[var(--text-muted)]">Health</dt>
        <dd>{unit.health}</dd>
        <dt className="text-[var(--text-muted)]">Location</dt>
        <dd>
          row {unit.row + 1} · rack {unit.rack + 1} · U{Math.floor(unit.u) + 1}
        </dd>
      </dl>
      <h3 className="mt-4 text-sm font-semibold">Fabric links</h3>
      {unit.links.length === 0 ? (
        <p className="text-sm text-[var(--text-muted)]">Blade: uplinks belong to its chassis.</p>
      ) : (
        <ul className="mt-1 space-y-1 text-sm">
          {unit.links.map((l, i) => (
            <li key={i} className="flex items-center gap-2">
              <StateDot state={l.state} />
              FI-{l.fabric === 0 ? "A" : "B"} port {l.port} · {formatSpeed(l.speedMbps)} · {l.state}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The server's detail page as a card over the 3D scene, so the walker keeps its place. */
export function ServerCard({ unit, onClose }: { unit: Unit; onClose: () => void }) {
  return (
    <CardShell label={`Server ${unit.name}`} onClose={onClose}>
      {unit.id.startsWith("demo-") ? <DemoDetails unit={unit} /> : <ServerDetailView id={unit.id} />}
    </CardShell>
  );
}
