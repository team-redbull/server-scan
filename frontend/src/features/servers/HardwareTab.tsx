import { HealthBadge } from "@/components/HealthBadge";
import { NOT_READ_TITLE, Reported, STALE_TITLE, UnconfirmedMarker } from "@/components/Reported";
import { isHealthSeverity } from "@/components/severity";
import type { ComponentHealth, HardwareInfo } from "@/types/server";

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

function formatBytes(bytes: number): string {
  if (bytes <= 0) {
    return "0 B";
  }
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), BYTE_UNITS.length - 1);
  const value = bytes / 1024 ** exponent;
  const unit = BYTE_UNITS[exponent] ?? "B";
  return `${value.toFixed(exponent === 0 ? 0 : 1)} ${unit}`;
}

export function HardwareTab({
  hardware,
  unreadFields = [],
}: {
  hardware: HardwareInfo | undefined;
  /** `ServerDetail.unread_fields`; optional for a document written before
   * the field existed. */
  unreadFields?: string[] | undefined;
}) {
  if (!hardware) {
    return <p className="text-gray-500">No hardware data available.</p>;
  }

  const unread = new Set(unreadFields);
  const { cpu, memory, storage, gpus, power } = hardware;

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">CPU</h2>
        {cpu ? (
          <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
            <Stat label="Model" value={cpu.model} unread={unread.has("hardware.cpu.model")} />
            <Stat label="Sockets" value={cpu.sockets} unread={unread.has("hardware.cpu.sockets")} />
            <Stat label="Cores" value={cpu.cores} unread={unread.has("hardware.cpu.cores")} />
            <Stat label="Threads" value={cpu.threads} unread={unread.has("hardware.cpu.threads")} />
          </dl>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No CPU data.</p>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Memory</h2>
        <Reported
          unread={unread.has("hardware.memory.total_bytes")}
          empty={!memory || memory.total_bytes <= 0}
        >
          {memory ? (
            <p className="mt-2 text-sm">
              {formatBytes(memory.total_bytes)} total ({memory.modules.length} module
              {memory.modules.length === 1 ? "" : "s"})
            </p>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No memory data.</p>
          )}
        </Reported>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Storage</h2>
        {/* Total on its own, decoupled from per-drive detail: nested inside
         * the drives block it hid behind "No storage data." whenever
         * `drives` alone was empty. */}
        <Reported
          unread={unread.has("hardware.storage.total_bytes")}
          empty={!storage || storage.total_bytes <= 0}
        >
          {storage ? (
            <p className="mt-2 text-sm">{formatBytes(storage.total_bytes)} total</p>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No storage data.</p>
          )}
        </Reported>
        <Reported
          unread={unread.has("hardware.storage.drives")}
          empty={!storage || storage.drives.length === 0}
        >
          {storage && storage.drives.length > 0 ? (
            <table className="mt-2 min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
              <thead>
                <tr className="text-left text-gray-500">
                  <th className="py-1 pr-4">Model</th>
                  <th className="py-1 pr-4">Serial</th>
                  <th className="py-1 pr-4">Media</th>
                  <th className="py-1 pr-4">Capacity</th>
                  <th className="py-1 pr-4">Health</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {storage.drives.map((drive) => (
                  <tr key={drive.id}>
                    <td className="py-1 pr-4">{drive.model ?? "—"}</td>
                    <td className="py-1 pr-4">{drive.serial ?? "—"}</td>
                    <td className="py-1 pr-4">{drive.media_type}</td>
                    <td className="py-1 pr-4">
                      {drive.capacity_bytes != null ? formatBytes(drive.capacity_bytes) : "—"}
                    </td>
                    <td className="py-1 pr-4">
                      <Health value={drive.health} detail={drive.health_detail} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No per-drive detail.</p>
          )}
        </Reported>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">GPU</h2>
        <Reported unread={unread.has("hardware.gpus")} empty={!gpus || gpus.length === 0}>
          {gpus && gpus.length > 0 ? (
            <div className="mt-2 overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
                <thead>
                  <tr className="text-left text-gray-500">
                    <th className="py-1 pr-4">Vendor</th>
                    <th className="py-1 pr-4">Model</th>
                    <th className="py-1 pr-4">Serial</th>
                    <th className="py-1 pr-4">VRAM</th>
                    <th className="py-1 pr-4">Memory type</th>
                    <th className="py-1 pr-4">ECC</th>
                    <th className="py-1 pr-4">Errors (correctable/uncorrectable)</th>
                    <th className="py-1 pr-4">Temp</th>
                    <th className="py-1 pr-4">Power</th>
                    <th className="py-1 pr-4">Health</th>
                    <th className="py-1 pr-4">Firmware</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                  {gpus.map((gpu, index) => (
                    <tr key={`${gpu.serial ?? gpu.model ?? "gpu"}-${index}`}>
                      <td className="py-1 pr-4">{gpu.vendor ?? "—"}</td>
                      <td className="py-1 pr-4">{gpu.model ?? "Unknown model"}</td>
                      <td className="py-1 pr-4">{gpu.serial ?? "—"}</td>
                      <td className="py-1 pr-4">
                        {gpu.memory_bytes != null ? formatBytes(gpu.memory_bytes) : "—"}
                      </td>
                      <td className="py-1 pr-4">{gpu.memory_type ?? "—"}</td>
                      <td className="py-1 pr-4">
                        {gpu.ecc_mode_enabled == null ? "—" : gpu.ecc_mode_enabled ? "On" : "Off"}
                      </td>
                      <td className="py-1 pr-4">
                        {gpu.correctable_error_count ?? "—"} / {gpu.uncorrectable_error_count ?? "—"}
                      </td>
                      <td className="py-1 pr-4">
                        {gpu.temperature_celsius != null
                          ? `${gpu.temperature_celsius.toFixed(0)}°C`
                          : "—"}
                      </td>
                      <td className="py-1 pr-4">
                        {gpu.power_watts != null ? `${gpu.power_watts.toFixed(0)}W` : "—"}
                      </td>
                      <td className="py-1 pr-4">
                        <Health value={gpu.health} detail={gpu.health_detail} />
                      </td>
                      <td className="py-1 pr-4">{gpu.firmware_version ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No GPUs.</p>
          )}
        </Reported>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Power</h2>
        <Reported
          unread={unread.has("hardware.power.psus")}
          empty={!power || power.psus.length === 0}
        >
          {power && power.psus.length > 0 ? (
            <ul className="mt-2 list-disc pl-5 text-sm">
              {power.psus.map((psu, index) => (
                <li key={psu.id || `psu-${index}`}>
                  {psu.model ?? psu.id ?? "—"}
                  {psu.capacity_watts != null ? ` — ${psu.capacity_watts}W` : ""}{" "}
                  <Health value={psu.health} detail={psu.health_detail} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No power supply data.</p>
          )}
        </Reported>
      </section>
    </div>
  );
}

/** A component's condition: badged when it is a `HealthSeverity`, the
 * collector's raw word otherwise (Cisco reports UP/DOWN), dashed when unread. */
function Health({ value, detail }: { value: ComponentHealth; detail?: string | null }) {
  if (value == null) {
    return <>—</>;
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      {isHealthSeverity(value) ? <HealthBadge severity={value} /> : <>{value}</>}
      {/* The raw vendor reason `value` was reduced from; diagnosis only. */}
      {detail && <span className="text-xs text-gray-500">({detail})</span>}
    </span>
  );
}

function Stat({
  label,
  value,
  unread = false,
}: {
  label: string;
  value: string | number | null | undefined;
  unread?: boolean;
}) {
  // Falsy catches the `0`/`""` `_carry_forward` writes for a never-read field.
  const empty = !value;
  const className = unread
    ? empty
      ? "font-medium text-gray-500"
      : "font-medium opacity-70"
    : "font-medium";
  return (
    <div>
      <dt className="text-xs text-gray-500">{label}</dt>
      <dd className={className} title={unread ? (empty ? NOT_READ_TITLE : STALE_TITLE) : undefined}>
        {unread && empty ? "Not reported" : (value ?? "—")}
        {unread && !empty && <UnconfirmedMarker />}
      </dd>
    </div>
  );
}
