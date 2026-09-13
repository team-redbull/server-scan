/** The sortable columns, one declaration for list and type. Every value
 * must be in the backend's `SORT_FIELDS` whitelist and covered by a
 * compound index (`app.domain.services.search`). */
export const SORTABLE_FIELDS = [
  "name",
  "model",
  "updated_at",
  "openshift_state",
  "cluster_name",
  "mce_name",
] as const;

export type SortableField = (typeof SORTABLE_FIELDS)[number];
