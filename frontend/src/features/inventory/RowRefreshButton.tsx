import type { MouseEvent } from "react";

import { useAuth } from "@/features/auth/useAuth";
import { RefreshIcon } from "@/components/RefreshIcon";
import { errorMessage } from "@/features/servers/refreshError";
import { useRefreshServerMutation } from "@/features/servers/hooks";
import { showToast } from "@/lib/toast";
import type { ServerRow } from "@/types/server";

const ADMIN_ONLY_TITLE = "Only admins can fetch a server's data";

/** The inventory row's "Fetch now": reads this one server from its vendor manager (ADR-0064),
 * the same call as the detail page's button. Icon-only, first column, admin only like
 * `MaintenanceToggle`; it stops the click so the row does not open the server. */
export function RowRefreshButton({ row }: { row: ServerRow }) {
  const { data: me } = useAuth();
  const isAdmin = me?.role !== "VIEWER" && me?.role !== "AUDITOR";
  const refresh = useRefreshServerMutation();
  const pending = refresh.isPending;
  const label = `Fetch data of ${row.name} now`;

  function fetchNow(event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    if (!isAdmin) return;
    refresh.mutate(row.id, {
      onSuccess: (fresh) => {
        showToast({
          title: `Fetched ${fresh.name}`,
          detail: `From ${fresh.source_provider ?? "its manager"}`,
          ok: true,
        });
      },
      onError: (error) => {
        showToast({ title: "Fetch failed", detail: `${row.name}: ${errorMessage(error)}`, ok: false });
      },
    });
  }

  return (
    <button
      type="button"
      title={isAdmin ? label : ADMIN_ONLY_TITLE}
      aria-label={isAdmin ? label : ADMIN_ONLY_TITLE}
      aria-disabled={!isAdmin || undefined}
      disabled={pending}
      onClick={fetchNow}
      className={`inline-flex size-6 items-center justify-center rounded-md border border-[var(--border-subtle)] text-[var(--text-muted)] transition-colors duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] disabled:cursor-wait disabled:opacity-60 ${
        !isAdmin ? "cursor-not-allowed opacity-40 hover:border-[var(--border-subtle)] hover:text-[var(--text-muted)]" : ""
      }`}
    >
      <RefreshIcon spinning={pending} />
    </button>
  );
}
