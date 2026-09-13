import type { LinkState } from "@/types/server";

const LINK_STATE_STYLES: Record<LinkState, string> = {
  UP: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
  DOWN: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  DISABLED: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
  UNKNOWN: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
};

const LINK_STATE_DOT: Record<LinkState, string> = {
  UP: "bg-green-500",
  DOWN: "bg-red-500",
  DISABLED: "bg-gray-400",
  UNKNOWN: "bg-gray-400",
};

export function LinkStateBadge({ state }: { state: LinkState }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${LINK_STATE_STYLES[state]}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${LINK_STATE_DOT[state]}`} aria-hidden="true" />
      {state}
    </span>
  );
}
