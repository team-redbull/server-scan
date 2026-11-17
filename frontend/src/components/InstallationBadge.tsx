import type { Installation } from "@/lib/installation";

/**
 * Whether a server is in use. The palette is inverted against every other
 * badge — red is "in use", green is "free to take" — so the label is always
 * text and no `SEVERITY_GLYPH` shape is borrowed. `MAINTENANCE` is a free
 * server in maintenance, which is not available (`installationOf`).
 */
const STYLES: Record<Installation, string> = {
  AVAILABLE: "bg-[var(--tint-healthy)] text-[var(--text-on-healthy)]",
  INSTALLED: "bg-[var(--tint-critical)] text-[var(--text-on-critical)]",
  INSTALLED_TO_INVENTORY: "bg-[var(--tint-info)] text-[var(--text-on-info)]",
  MAINTENANCE: "bg-[var(--tint-maintenance)] text-[var(--text-on-maintenance)]",
};

/** Table-width labels. */
const SHORT_LABELS: Record<Installation, string> = {
  AVAILABLE: "Available",
  INSTALLED: "Installed",
  INSTALLED_TO_INVENTORY: "In inventory",
  MAINTENANCE: "Maintenance",
};

/** Detail-page labels. */
const FULL_LABELS: Record<Installation, string> = {
  AVAILABLE: "Available",
  INSTALLED: "Installed",
  INSTALLED_TO_INVENTORY: "Installed to inventory",
  MAINTENANCE: "In maintenance",
};

export function InstallationBadge({
  state,
  full = false,
}: {
  state: Installation;
  full?: boolean;
}) {
  const labels = full ? FULL_LABELS : SHORT_LABELS;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STYLES[state]}`}
    >
      {labels[state]}
    </span>
  );
}
