import type { ServerRow } from "@/types/server";

/** Column order, header text, and value accessor for the inventory CSV export. */
const COLUMNS: { header: string; value: (row: ServerRow) => string }[] = [
  { header: "Name", value: (r) => r.name },
  { header: "BMC address", value: (r) => r.bmc_host ?? "" },
  { header: "Installation", value: (r) => r.openshift_state },
  { header: "MCE", value: (r) => r.mce_name ?? "" },
  { header: "Cluster", value: (r) => r.cluster_name ?? "" },
  { header: "Model", value: (r) => r.model ?? "" },
  { header: "Serial", value: (r) => r.serial ?? "" },
  { header: "SPT", value: (r) => r.profile_template_name ?? "" },
  { header: "State", value: (r) => r.health },
];

/** Quotes a field only when it needs it (RFC 4180), doubling embedded quotes. */
function csvField(value: string): string {
  return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/** Renders `rows` as CSV text (header row, CRLF line endings) in the order given —
 * the caller decides which filter/sort state that reflects. */
export function rowsToCsv(rows: ServerRow[]): string {
  const lines = [COLUMNS.map((c) => csvField(c.header)).join(",")];
  for (const row of rows) {
    lines.push(COLUMNS.map((c) => csvField(c.value(row))).join(","));
  }
  return lines.join("\r\n");
}

/** Triggers a browser download of `content` as a file named `filename`. */
export function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
