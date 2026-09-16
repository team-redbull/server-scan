import { UNASSIGNED_SITE_ID } from "@/api/sites";
import type { SortableField } from "@/features/inventory/sorting";
import type { ServerRow } from "@/types/server";
import { SEVERITY_ORDER } from "@/components/severity";

/**
 * Filtering, search, sort, facet counts and paging over the fleet's rows —
 * pure functions, so the page is `useMemo` glue (ADR-0033).
 */

export interface RowFilters {
  search?: string;
  vendor?: string;
  site_id?: string;
  source_provider?: string;
  installation_type?: string;
  openshift_state?: string;
  health?: string;
  maintenance?: true;
  stale?: true;
  duplicate?: true;
}

export interface FacetCounts {
  total: number;
  vendor: Record<string, number>;
  source_provider: Record<string, number>;
  installation_type: Record<string, number>;
  site_id: Record<string, number>;
  openshift_state: Record<string, number>;
  health: Record<string, number>;
  maintenance: Record<string, number>;
  stale: Record<string, number>;
}

const COLLATOR = new Intl.Collator("en", {
  numeric: true,
  sensitivity: "base",
});

const HEX_LIKE = /^[0-9a-f:-]+$/i;

/**
 * How many rows in the given set share each name — the estate-side
 * naming collisions a name-derived duplicate can't tell apart from a
 * genuine platform bug (docs/adr/0016's duplicate-server update).
 *
 * Args:
 *   rows (ServerRow[]): The set to count within — the whole fleet, not
 *     an already-filtered subset, or a pair split by another filter
 *     would each look unique.
 *
 * Returns:
 *   Map<string, number>: Row count per `name`.
 */
function nameCounts(rows: ServerRow[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const row of rows) {
    counts.set(row.name, (counts.get(row.name) ?? 0) + 1);
  }
  return counts;
}

/**
 * Keep the rows every set filter matches; `search` is applied last.
 *
 * Args:
 *   rows (ServerRow[]): The fleet — always the whole thing, even when a
 *     `duplicate` filter is also set (see `nameCounts`).
 *   filters (RowFilters): Empty strings and unset keys are "any".
 *
 * Returns:
 *   ServerRow[]: The matching rows, in input order.
 */
export function filterRows(
  rows: ServerRow[],
  filters: RowFilters,
): ServerRow[] {
  const counts = filters.duplicate ? nameCounts(rows) : null;
  const kept = rows.filter(
    (row) =>
      (!filters.vendor || row.vendor === filters.vendor) &&
      (!filters.site_id ||
        (row.site_id ?? UNASSIGNED_SITE_ID) === filters.site_id) &&
      (!filters.source_provider ||
        row.source_provider === filters.source_provider) &&
      (!filters.installation_type ||
        row.installation_type === filters.installation_type) &&
      (!filters.openshift_state ||
        row.openshift_state === filters.openshift_state) &&
      (!filters.health || row.health === filters.health) &&
      (!filters.maintenance || row.maintenance.enabled) &&
      (!filters.stale || row.stale) &&
      (!counts || (counts.get(row.name) ?? 0) > 1),
  );
  return searchRows(kept, filters.search ?? "");
}

/**
 * Case-insensitive substring search over the same fields the API's token
 * search indexes — name, model, serial, vendor, site, installation type,
 * BMC host and MACs; a hex-looking query also matches MACs bare.
 *
 * Args:
 *   rows (ServerRow[]): The rows to search.
 *   query (string): Blank means "keep everything".
 *
 * Returns:
 *   ServerRow[]: The rows with a match, in input order.
 */
export function searchRows(rows: ServerRow[], query: string): ServerRow[] {
  const q = query.trim().toLowerCase();
  if (!q) return rows;
  const bare = HEX_LIKE.test(q) ? q.replace(/[:-]/g, "") : null;
  return rows.filter((row) => {
    if (row.name.toLowerCase().includes(q)) return true;
    if (row.model?.toLowerCase().includes(q)) return true;
    if (row.serial?.toLowerCase().includes(q)) return true;
    if (row.vendor.includes(q)) return true;
    if (row.site_id?.toLowerCase().includes(q)) return true;
    if (row.installation_type.toLowerCase().includes(q)) return true;
    if (row.bmc_host?.toLowerCase().includes(q)) return true;
    return row.macs.some(
      (mac) =>
        mac.includes(q) ||
        (bare !== null && mac.replace(/:/g, "").includes(bare)),
    );
  });
}

/**
 * Sort a copy of the rows by one column, naturally (`srv-2` before
 * `srv-10`) and with nulls last whichever the direction; `health` sorts
 * by severity (ascending = healthiest first), not alphabetically.
 *
 * Args:
 *   rows (ServerRow[]): The rows; not mutated.
 *   field (SortableField): The column.
 *   desc (boolean): Descending when true.
 *
 * Returns:
 *   ServerRow[]: A sorted copy. Ties keep input order.
 */
export function sortRows(
  rows: ServerRow[],
  field: SortableField,
  desc: boolean,
): ServerRow[] {
  const sign = desc ? -1 : 1;
  if (field === "health") {
    const rank = (row: ServerRow) =>
      SEVERITY_ORDER.length - SEVERITY_ORDER.indexOf(row.health);
    return [...rows].sort((a, b) => sign * (rank(a) - rank(b)));
  }
  return [...rows].sort((a, b) => {
    const av = a[field];
    const bv = b[field];
    if (av === null) return bv === null ? 0 : 1;
    if (bv === null) return -1;
    return sign * COLLATOR.compare(av, bv);
  });
}

function count(counts: Record<string, number>, key: string): void {
  counts[key] = (counts[key] ?? 0) + 1;
}

/**
 * Per-option match counts over the rows given — call it with the filtered
 * set, so each count describes what the operator is looking at.
 *
 * Args:
 *   rows (ServerRow[]): The rows to count.
 *
 * Returns:
 *   FacetCounts: Per-value counts; a value no row has is absent (the page
 *   renders it as 0).
 */
export function facetCounts(rows: ServerRow[]): FacetCounts {
  const facets: FacetCounts = {
    total: rows.length,
    vendor: {},
    source_provider: {},
    installation_type: {},
    site_id: {},
    openshift_state: {},
    health: {},
    maintenance: {},
    stale: {},
  };
  for (const row of rows) {
    count(facets.vendor, row.vendor);
    if (row.source_provider) count(facets.source_provider, row.source_provider);
    count(facets.installation_type, row.installation_type);
    count(facets.site_id, row.site_id ?? UNASSIGNED_SITE_ID);
    count(facets.openshift_state, row.openshift_state);
    count(facets.health, row.health);
    count(facets.maintenance, String(row.maintenance.enabled));
    count(facets.stale, String(row.stale));
  }
  return facets;
}

/**
 * One page of rows, clamping an out-of-range page number.
 *
 * Args:
 *   rows (ServerRow[]): The full ordered set.
 *   page (number): 1-based; anything below 1 or past the end clamps.
 *   pageSize (number): Rows per page.
 *
 * Returns:
 *   The page's rows, the page actually shown, and the page count (never 0).
 */
export function paginate(
  rows: ServerRow[],
  page: number,
  pageSize: number,
): { items: ServerRow[]; page: number; pageCount: number } {
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(Math.max(1, Math.trunc(page) || 1), pageCount);
  const start = (current - 1) * pageSize;
  return {
    items: rows.slice(start, start + pageSize),
    page: current,
    pageCount,
  };
}
