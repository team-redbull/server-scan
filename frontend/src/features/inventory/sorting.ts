/** The sortable columns, one declaration for list and type. Sorting is
 * client-side (`rows.ts`); nothing here reaches the backend. */
export const SORTABLE_FIELDS = ["name", "model", "openshift_state", "cluster_name", "mce_name"] as const;

export type SortableField = (typeof SORTABLE_FIELDS)[number];
