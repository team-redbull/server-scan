/** The sortable columns, one declaration for list and type. Sorting is
 * client-side (`rows.ts`); nothing here reaches the backend. */
export const SORTABLE_FIELDS = [
  "name",
  "model",
  "bmc_host",
  "openshift_state",
  "cluster_name",
  "mce_name",
  "health",
] as const;

export type SortableField = (typeof SORTABLE_FIELDS)[number];

/** An IPv4 address or the start of one: `10.0`, `10.0.0.1` (a bare number is not one). */
const ADDRESS_SEARCH = /^\d{1,3}(\.\d{0,3}){1,3}$/;

/**
 * The column a list is sorted by when the URL names none: BMC address for a search that is an
 * address (so `10.0.0.1` lists `.1`, `.10`, `.11` in order), otherwise the name.
 *
 * Args:
 *   search (string): The search box's text.
 *
 * Returns:
 *   SortableField: `bmc_host` for an address search, else `name`.
 */
export function defaultSortField(search: string): SortableField {
  return ADDRESS_SEARCH.test(search.trim()) ? "bmc_host" : "name";
}
