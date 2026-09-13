import type { OpenShiftState } from "@/types/server";

/**
 * Whether a server is in use. The palette is inverted against every other
 * badge — red is "in use", green is "free to take" — so the label is always
 * text and no `SEVERITY_GLYPH` shape is borrowed.
 */
const STYLES: Record<OpenShiftState, string> = {
  AVAILABLE: "bg-[var(--tint-healthy)] text-[var(--text-on-healthy)]",
  INSTALLED: "bg-[var(--tint-critical)] text-[var(--text-on-critical)]",
  INSTALLED_TO_INVENTORY: "bg-[var(--tint-info)] text-[var(--text-on-info)]",
};

/** Table-width labels. */
const SHORT_LABELS: Record<OpenShiftState, string> = {
  AVAILABLE: "Available",
  INSTALLED: "Installed",
  INSTALLED_TO_INVENTORY: "In inventory",
};

/** Detail-page labels. */
const FULL_LABELS: Record<OpenShiftState, string> = {
  AVAILABLE: "Available",
  INSTALLED: "Installed",
  INSTALLED_TO_INVENTORY: "Installed to inventory",
};

export function InstallationBadge({
  state,
  full = false,
}: {
  state: OpenShiftState;
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
