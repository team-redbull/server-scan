import { Fragment, useState } from "react";
import type { ReactNode } from "react";

import { ApiError } from "@/api/client";
import type { Conf } from "@/api/confs";
import { Badge } from "@/components/Badge";
import { useConfsQuery } from "@/features/confs/hooks";
import {
  UNKNOWN,
  formatGhz,
  formatKb,
  formatTb,
  gpuEntries,
  hasVram,
  totalStorageTb,
  visibleConfs,
} from "@/features/confs/format";

const FIELD_CLASS =
  "w-full min-w-0 rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1.5 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]";

const COLUMNS = ["Conf", "Part number", "Model", "CPU", "RAM", "Storage", "GPU", "Adapters"];

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail;
  if (error instanceof Error) return error.message;
  return "Failed to load confs.";
}

/** Text the writer recorded as `"unknown"` renders muted, so it reads as a value, not a gap. */
function Value({ text }: { text: string }) {
  if (text === UNKNOWN) {
    return <span className="italic text-[var(--text-muted)]">unknown</span>;
  }
  return <>{text}</>;
}

function Secondary({ children }: { children: ReactNode }) {
  return <div className="text-xs text-[var(--text-muted)]">{children}</div>;
}

function None() {
  return <span className="text-[var(--text-muted)]">—</span>;
}

function RamUnread() {
  return <span className="italic text-[var(--text-muted)]">not read (scan failed)</span>;
}

function ramTypeText(conf: Conf): string {
  return conf.ram.type.length > 0 ? conf.ram.type.join("/") : "type unread";
}

function CountedList({ entries }: { entries: { count: number; model: string }[] }) {
  if (entries.length === 0) return <None />;
  return (
    <ul className="space-y-0.5">
      {entries.map((entry) => (
        <li key={entry.model}>
          {entry.count}× <Value text={entry.model} />
        </li>
      ))}
    </ul>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
        {title}
      </h3>
      <div className="mt-1 space-y-0.5 text-sm">{children}</div>
    </section>
  );
}

/** Everything the table row abbreviates, laid out under it. */
function ConfDetail({ conf }: { conf: Conf }) {
  const { cpu, ram } = conf;
  const gpus = gpuEntries(conf);
  return (
    <div className="grid gap-6 bg-[var(--surface-sunken)] px-4 py-4 sm:grid-cols-2 lg:grid-cols-4">
      <DetailSection title="Identity">
        <div>Conf ID: <span className="font-mono">{conf.conf_id}</span></div>
        <div>Part number: {conf.part_number ? <span className="font-mono">{conf.part_number}</span> : <None />}</div>
        <div>Model: {conf.model}</div>
      </DetailSection>
      <DetailSection title="CPU">
        <div>{cpu.model}</div>
        <div>Vendor: {cpu.vendor ?? <None />}</div>
        <div>
          {cpu.sockets} socket{cpu.sockets === 1 ? "" : "s"} × {cpu.cores_per_cpu} cores
        </div>
        <div>{cpu.total_cores_ht} threads (HT)</div>
        <div>
          {formatGhz(cpu.clock_mhz)} base, {formatGhz(cpu.max_clock_mhz)} max
        </div>
        <div>
          Cache: L1 {formatKb(cpu.cache_kb.l1)} · L2 {formatKb(cpu.cache_kb.l2)} · L3{" "}
          {formatKb(cpu.cache_kb.l3)}
        </div>
      </DetailSection>
      <DetailSection title="Memory">
        {ram.total_gib === 0 ? (
          <RamUnread />
        ) : (
          <>
            <div>{ram.total_gib} GiB</div>
            <div>
              {ram.dimms} × {ram.per_dimm_gb} GB DIMMs
            </div>
            <div>
              Type:{" "}
              {ram.type.length > 0 ? ram.type.join(", ") : (
                <span className="italic text-[var(--text-muted)]">not read (DIMM read failed)</span>
              )}
            </div>
          </>
        )}
      </DetailSection>
      <DetailSection title="Storage, GPU and adapters">
        {conf.disks.length === 0 && <div>No disks</div>}
        {conf.disks.map((disk, index) => (
          <div key={`${disk.model}-${String(index)}`}>
            {disk.count}× <Value text={disk.model} /> · {formatTb(disk.capacity_tb)} ·{" "}
            <Value text={disk.type} /> · <Value text={disk.protocol} />
          </div>
        ))}
        {gpus.length > 0 && (
          <>
            <div className="pt-1">GPUs ({conf.gpu_count}):</div>
            <CountedList entries={gpus} />
            {hasVram(conf) && <div>{conf.gpu_vram_gb} GB VRAM per GPU</div>}
          </>
        )}
        <div className="pt-1">Adapters ({conf.adapter_count}):</div>
        <CountedList entries={conf.adapters} />
      </DetailSection>
    </div>
  );
}

function ConfRow({
  conf,
  expanded,
  onToggle,
}: {
  conf: Conf;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { cpu, ram } = conf;
  return (
    <Fragment>
      <tr
        onClick={onToggle}
        className="cursor-pointer align-top hover:bg-[var(--surface-hover)]"
      >
        <td className="whitespace-nowrap px-3 py-2">
          <button
            type="button"
            aria-expanded={expanded}
            aria-label={`${expanded ? "Hide" : "Show"} details of ${conf.conf_id}`}
            onClick={(event) => {
              event.stopPropagation();
              onToggle();
            }}
            className="mr-1.5 inline-block w-3 text-[var(--text-muted)]"
          >
            {expanded ? "▾" : "▸"}
          </button>
          <span className="font-mono font-medium">{conf.conf_id}</span>
        </td>
        <td className="px-3 py-2">
          {conf.part_number ? (
            <span className="font-mono">{conf.part_number}</span>
          ) : (
            <Badge tone="warning">Unmapped</Badge>
          )}
        </td>
        <td className="whitespace-nowrap px-3 py-2">{conf.model}</td>
        <td className="px-3 py-2">
          <div>
            {cpu.sockets}× {cpu.model}
          </div>
          <Secondary>
            {cpu.sockets * cpu.cores_per_cpu} cores · {cpu.total_cores_ht} threads ·{" "}
            {formatGhz(cpu.clock_mhz)}
          </Secondary>
        </td>
        <td className="whitespace-nowrap px-3 py-2">
          {ram.total_gib === 0 ? (
            <RamUnread />
          ) : (
            <>
              <div>{ram.total_gib} GiB</div>
              <Secondary>
                {ram.dimms} × {ram.per_dimm_gb} GB {ramTypeText(conf)}
              </Secondary>
            </>
          )}
        </td>
        <td className="whitespace-nowrap px-3 py-2">
          {conf.disks.length === 0 ? (
            <None />
          ) : (
            <>
              <div>{formatTb(totalStorageTb(conf))}</div>
              <Secondary>
                {conf.disks.reduce((sum, disk) => sum + disk.count, 0)} drives
              </Secondary>
            </>
          )}
        </td>
        <td className="px-3 py-2">
          <CountedList entries={gpuEntries(conf)} />
          {hasVram(conf) && <Secondary>{conf.gpu_vram_gb} GB each</Secondary>}
        </td>
        <td className="px-3 py-2">
          <CountedList entries={conf.adapters} />
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={COLUMNS.length} className="p-0">
            <ConfDetail conf={conf} />
          </td>
        </tr>
      )}
    </Fragment>
  );
}

/** `/confs`: every server configuration (spec). */
export function ConfsPage() {
  const query = useConfsQuery();
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());

  const items = query.data?.items ?? [];
  const unreadable = query.data?.unreadable ?? [];
  const rows = visibleConfs(items, search);

  function toggle(confId: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(confId)) next.add(confId);
      return next;
    });
  }

  return (
    <main className="mx-auto max-w-7xl p-8">
      <h1 className="text-2xl font-semibold">Confs</h1>
      <p className="mt-1 max-w-3xl text-sm text-[var(--text-secondary)]">
        Server configurations (specs). Read-only: the{" "}
        <code className="font-mono text-xs">confs</code> collection is written outside Server
        Scan. Click a row for the full spec.
      </p>

      {query.isPending && <p className="mt-6 text-[var(--text-muted)]">Loading…</p>}
      {query.isError && (
        <p className="mt-6 rounded border border-red-800 bg-red-950 p-3 text-red-300">
          {errorText(query.error)}
        </p>
      )}

      {query.isSuccess && (
        <>
          <div className="mt-6 flex flex-wrap items-end gap-4">
            <div className="flex min-w-64 flex-1 flex-col">
              <label htmlFor="confs-search" className="text-sm text-[var(--text-secondary)]">
                Search
              </label>
              <input
                id="confs-search"
                type="search"
                value={search}
                placeholder="Conf ID, part number, model, CPU, GPU, disk or adapter"
                onChange={(event) => {
                  setSearch(event.target.value);
                }}
                className={`mt-1 ${FIELD_CLASS}`}
              />
            </div>
            <p className="pb-1.5 text-sm text-[var(--text-secondary)]" aria-live="polite">
              {rows.length === items.length
                ? `${String(items.length)} confs`
                : `${String(rows.length)} of ${String(items.length)} confs`}
            </p>
          </div>

          {unreadable.length > 0 && (
            <p className="mt-4 rounded border border-amber-800 bg-amber-950/60 p-3 text-sm text-amber-300">
              {unreadable.length} stored conf{unreadable.length === 1 ? "" : "s"} could not be
              read and {unreadable.length === 1 ? "is" : "are"} not listed:{" "}
              <span className="font-mono">{unreadable.join(", ")}</span>
            </p>
          )}

          <div className="mt-4 overflow-x-auto rounded-lg border border-[var(--border-subtle)]">
            <table className="min-w-full divide-y divide-[var(--border-subtle)] text-sm">
              <thead className="bg-[var(--surface-raised)]">
                <tr>
                  {COLUMNS.map((column) => (
                    <th
                      key={column}
                      scope="col"
                      className="px-3 py-2 text-left font-medium text-[var(--text-secondary)]"
                    >
                      {column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {rows.map((conf, index) => (
                  <ConfRow
                    key={`${conf.conf_id}#${String(index)}`}
                    conf={conf}
                    expanded={expanded.has(conf.conf_id)}
                    onToggle={() => {
                      toggle(conf.conf_id);
                    }}
                  />
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td
                      colSpan={COLUMNS.length}
                      className="px-3 py-6 text-center text-[var(--text-muted)]"
                    >
                      {items.length === 0 ? "No confs are stored." : "No conf matches the search."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </main>
  );
}
