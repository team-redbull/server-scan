import { useState } from "react";

import type { ClusterFacets, ClusterOption } from "@/features/inventory/rows";

/**
 * The MCE / hosted-cluster / UPI-cluster filter, built from the fleet itself
 * (`clusterFacets`). Three multi-select sections, each hidden while empty;
 * a box narrows the two cluster lists, which can run to dozens.
 */

interface ClusterSidebarProps {
  facets: ClusterFacets;
  mce: string[];
  cluster: string[];
  onToggle: (key: "mce" | "cluster", value: string) => void;
}

function Section({
  title,
  options,
  selected,
  paramKey,
  onToggle,
}: {
  title: string;
  options: ClusterOption[];
  selected: string[];
  paramKey: "mce" | "cluster";
  onToggle: ClusterSidebarProps["onToggle"];
}) {
  if (options.length === 0) return null;
  return (
    <section aria-label={title}>
      <h2 className="text-xs font-medium tracking-wide text-[var(--text-secondary)] uppercase">
        {title}{" "}
        <span className="text-[var(--text-muted)]">({options.length})</span>
      </h2>
      <ul className="mt-1.5 space-y-0.5">
        {options.map((option) => (
          <li key={option.name}>
            <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-xs text-[var(--text-primary)] hover:bg-[var(--surface-hover)]">
              <input
                type="checkbox"
                checked={selected.includes(option.name)}
                onChange={() => {
                  onToggle(paramKey, option.name);
                }}
              />
              <span
                className="min-w-0 flex-1 truncate"
                title={
                  option.mce ? `${option.name} · ${option.mce}` : option.name
                }
              >
                {option.name}
              </span>
              <span className="text-[var(--text-muted)]">{option.count}</span>
            </label>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ClusterSidebar({
  facets,
  mce,
  cluster,
  onToggle,
}: ClusterSidebarProps) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(true);
  const q = query.trim().toLowerCase();
  const narrow = (options: ClusterOption[]) =>
    q ? options.filter((o) => o.name.toLowerCase().includes(q)) : options;

  const { mces, hosted, upi } = facets;
  if (mces.length + hosted.length + upi.length === 0) return null;

  if (!open) {
    const ticked = mce.length + cluster.length;
    return (
      <button
        type="button"
        aria-expanded={false}
        onClick={() => {
          setOpen(true);
        }}
        className="shrink-0 self-start rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1.5 text-xs font-medium text-[var(--text-secondary)] hover:border-[var(--border-strong)]"
      >
        Clusters{ticked > 0 ? ` (${ticked})` : ""} ›
      </button>
    );
  }

  return (
    <aside
      aria-label="Cluster filters"
      className="sticky top-4 max-h-[calc(100vh-2rem)] w-48 shrink-0 space-y-4 self-start overflow-y-auto rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-3"
    >
      <button
        type="button"
        aria-expanded
        onClick={() => {
          setOpen(false);
        }}
        className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]"
      >
        ‹ Hide filters
      </button>
      <Section
        title="MCE"
        options={mces}
        selected={mce}
        paramKey="mce"
        onToggle={onToggle}
      />
      {hosted.length + upi.length > 8 && (
        <input
          type="search"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
          }}
          placeholder="Find a cluster…"
          aria-label="Find a cluster"
          className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-2 py-1 text-xs text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]"
        />
      )}
      <Section
        title="Hosted clusters"
        options={narrow(hosted)}
        selected={cluster}
        paramKey="cluster"
        onToggle={onToggle}
      />
      <Section
        title="UPI clusters"
        options={narrow(upi)}
        selected={cluster}
        paramKey="cluster"
        onToggle={onToggle}
      />
    </aside>
  );
}
