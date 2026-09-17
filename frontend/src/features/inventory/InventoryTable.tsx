import { flexRender } from "@tanstack/react-table";
import { useMemo } from "react";
import type { SortingState } from "@tanstack/react-table";
// v9's `/legacy` entry point keeps the v8 hook shape (`useReactTable` /
// `createColumnHelper`) — the officially supported migration path, not a
// workaround, so this builds on it rather than the `useTable` + `features` API.
import { legacyCreateColumnHelper, useLegacyTable } from "@tanstack/react-table/legacy";
import type { LegacyColumnDef } from "@tanstack/react-table/legacy";
import { Link, useLocation, useNavigate } from "react-router";

import type { SortableField } from "@/features/inventory/sorting";
import { InstallationBadge } from "@/components/InstallationBadge";
import { MaintenanceToggle } from "@/features/inventory/MaintenanceToggle";
import { StateBadge } from "@/components/StateBadge";
import type { HealthSeverity, OpenShiftState } from "@/types/server";
import type { ServerRow } from "@/types/server";

/**
 * The inventory columns. Name is left-aligned, everything else centred;
 * MCE renders only when a row on the page has one; the maintenance switch
 * is the rightmost column and the row's only write control. Rows animate
 * nothing — only the background responds to hover.
 */

/** A left edge on the rows that need attention, nothing on the rest. */
const ROW_ACCENT: Record<HealthSeverity, string> = {
  CRITICAL: "border-l-2 border-l-[var(--color-status-critical)]",
  MAJOR: "border-l-2 border-l-[var(--text-on-major)]",
  WARNING: "border-l-2 border-l-[var(--color-status-warning)]",
  HEALTHY: "border-l-2 border-l-transparent",
  UNKNOWN: "border-l-2 border-l-transparent",
};

interface InventoryTableProps {
  servers: ServerRow[];
  sortField: SortableField;
  sortDesc: boolean;
  onSortChange: (field: SortableField, desc: boolean) => void;
  /** Names the active filters in the empty state. */
  emptyMessage?: string;
}

const columnHelper = legacyCreateColumnHelper<ServerRow>();

// Columns have heterogeneous `TValue`; TanStack's docs recommend
// `ColumnDef<TData, any>` for exactly this case — a `TValue=unknown` array
// is rejected by `exactOptionalPropertyTypes` on every column.
function buildColumns(withMce: boolean, from: string): LegacyColumnDef<ServerRow, any>[] {
  return [
  columnHelper.accessor("name", {
    id: "name",
    header: "Name",
    cell: (info) => (
      // A real anchor; the row's `onClick` is a convenience on top of it.
      // `state.from` is this page's own URL (filters included), so the
      // detail page's "Back to inventory" returns to the same filtered view.
      <Link
        to={`/servers/${info.row.original.id}`}
        state={{ from }}
        className="font-medium text-[var(--text-primary)] underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)]"
      >
        {info.getValue()}
      </Link>
    ),
    enableSorting: true,
  }),
  columnHelper.accessor("openshift_state", {
    id: "openshift_state",
    header: "Installation",
    cell: (info) => <InstallationBadge state={info.getValue<OpenShiftState>()} />,
    enableSorting: true,
  }),
  ...(withMce
    ? [
        columnHelper.accessor("mce_name", {
          id: "mce_name",
          header: "MCE",
          cell: (info) => (
            <span className="text-[var(--text-secondary)]">{info.getValue() || "—"}</span>
          ),
          enableSorting: true,
        }),
      ]
    : []),
  columnHelper.accessor("cluster_name", {
    id: "cluster_name",
    header: "Cluster",
    cell: (info) => (
      <span className="text-[var(--text-secondary)]">{info.getValue() || "—"}</span>
    ),
    enableSorting: true,
  }),
  columnHelper.accessor("model", {
    id: "model",
    header: "Model",
    cell: (info) => (
      <span className="text-[var(--text-secondary)]">{info.getValue() || "—"}</span>
    ),
    enableSorting: true,
  }),
  columnHelper.accessor((row) => row, {
    id: "health",
    header: "State",
    cell: (info) => {
      const row = info.getValue<ServerRow>();
      return (
        <StateBadge
          severity={row.health}
          maintenance={row.maintenance}
          stale={row.stale}
          lastSeenAt={row.last_seen_at}
        />
      );
    },
    enableSorting: true,
  }),
  // The only cell whose click does not open the server.
  columnHelper.accessor((row) => row, {
    id: "maintenance",
    header: "Maintenance",
    cell: (info) => <MaintenanceToggle server={info.getValue<ServerRow>()} />,
    enableSorting: false,
  }),
  ];
}

export function InventoryTable({
  servers,
  sortField,
  sortDesc,
  onSortChange,
  emptyMessage,
}: InventoryTableProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const from = `${location.pathname}${location.search}`;
  const sorting: SortingState = [{ id: sortField, desc: sortDesc }];
  const withMce = servers.some((server) => server.mce_name);
  const columns = useMemo(() => buildColumns(withMce, from), [withMce, from]);

  const table = useLegacyTable({
    data: servers,
    columns,
    state: { sorting },
    manualSorting: true,
    enableSortingRemoval: false,
    onSortingChange: (updater) => {
      const next = typeof updater === "function" ? updater(sorting) : updater;
      const first = next[0];
      if (!first) {
        return;
      }
      onSortChange(first.id as SortableField, first.desc);
    },
    getRowId: (row) => row.id,
  });

  return (
    // `overflow-x-auto`, never `overflow-hidden` (clips the row's only
    // control) and `-x-` alone: any `overflow-y` but `visible` makes this
    // div the sticky header's scroll block, and it never scrolls — the page does.
    <div className="overflow-x-auto rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)]">
      <table className="min-w-full text-sm">
        <thead className="sticky top-0 z-10 bg-[var(--surface-sunken)]">
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header, index) => (
                <th
                  key={header.id}
                  scope="col"
                  className={`border-b border-[var(--border-subtle)] px-2 py-2.5 text-xs font-medium tracking-wide text-[var(--text-secondary)] uppercase ${header.column.id === "name" ? "text-left" : "text-center"} ${index === 0 ? "rounded-tl-[var(--radius-card)]" : ""} ${index === headerGroup.headers.length - 1 ? "rounded-tr-[var(--radius-card)]" : ""}`}
                >
                  {header.column.getCanSort() ? (
                    <button
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                      // `uppercase` repeated: Tailwind preflight resets
                      // `text-transform` on <button>.
                      className="inline-flex items-center gap-1 rounded-sm uppercase hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)]"
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      <span aria-hidden="true" className="text-[0.65rem]">
                        {header.column.getIsSorted() === "asc" && "▲"}
                        {header.column.getIsSorted() === "desc" && "▼"}
                      </span>
                    </button>
                  ) : (
                    flexRender(header.column.columnDef.header, header.getContext())
                  )}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              onClick={(event) => {
                // Never hijack the anchor's own click or a modified one.
                if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) {
                  return;
                }
                if ((event.target as HTMLElement).closest("a")) {
                  return;
                }
                void navigate(`/servers/${row.original.id}`, { state: { from } });
              }}
              className={`group cursor-pointer border-b border-[var(--border-subtle)] transition-colors duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] last:border-0 hover:bg-[var(--surface-hover)] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--color-status-info)] ${ROW_ACCENT[row.original.health]}`}
            >
              {row.getVisibleCells().map((cell) => (
                <td
                  key={cell.id}
                  className={`px-2 py-2.5 whitespace-nowrap ${cell.column.id === "name" ? "text-left" : "text-center"}`}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
          {servers.length === 0 && (
            <tr>
              <td
                colSpan={columns.length}
                className="px-3 py-12 text-center text-sm text-[var(--text-muted)]"
              >
                {emptyMessage ?? "No servers match the current filters."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
