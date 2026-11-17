import { ApiError } from "@/api/client";
import { useAuth } from "@/features/auth/useAuth";
import { useRefreshServerMutation } from "@/features/servers/hooks";
import { showToast } from "@/lib/toast";
import type { ServerDetail } from "@/types/server";

const ADMIN_ONLY_TITLE = "Only admins can fetch a server's data";
const TITLE = "Read this server from its vendor manager now, instead of waiting for the collector";

function RefreshIcon({ spinning }: { spinning: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={`size-3.5 fill-none stroke-current ${spinning ? "animate-spin" : ""}`}
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" strokeLinecap="round" />
      <path d="M12.5 1.5v3h-3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem.detail;
  }
  return error instanceof Error ? error.message : "Failed to fetch the server.";
}

/** The detail header's "Fetch now": one server read from its manager, not the whole
 * collector run (ADR-0062). Admin only, gated like `MaintenanceToggle`. */
export function RefreshButton({ server }: { server: ServerDetail }) {
  const { data: me } = useAuth();
  const isAdmin = me?.role !== "VIEWER" && me?.role !== "AUDITOR";
  const refresh = useRefreshServerMutation();
  const pending = refresh.isPending;

  function fetchNow() {
    // `aria-disabled`, not `disabled`, for a viewer, so the tooltip still shows.
    if (!isAdmin) return;
    const before = server.revision;
    refresh.mutate(server.id, {
      onSuccess: (fresh) => {
        showToast({
          title: `Fetched ${fresh.name}`,
          detail: `From ${fresh.source_provider ?? "its manager"}${
            fresh.revision === before ? " — no changes" : " — data updated"
          }`,
          ok: true,
        });
      },
      onError: () => {
        showToast({ title: "Fetch failed", detail: server.name, ok: false });
      },
    });
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button
        type="button"
        title={isAdmin ? TITLE : ADMIN_ONLY_TITLE}
        aria-label={isAdmin ? "Fetch this server's data now" : ADMIN_ONLY_TITLE}
        aria-disabled={!isAdmin || undefined}
        disabled={pending}
        onClick={fetchNow}
        className={`inline-flex h-7 items-center justify-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-2.5 text-xs font-medium text-[var(--text-muted)] transition-colors duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] disabled:cursor-wait disabled:opacity-60 ${
          !isAdmin
            ? "cursor-not-allowed opacity-40 hover:border-[var(--border-subtle)] hover:text-[var(--text-muted)]"
            : ""
        }`}
      >
        <RefreshIcon spinning={pending} />
        {pending ? "Fetching…" : "Fetch now"}
      </button>
      {/* Inline as well as the toast: the reason (e.g. "run its collector job instead") outlives 2 s. */}
      {refresh.isError && (
        <span role="alert" className="text-xs text-[var(--text-on-critical)]">
          {errorMessage(refresh.error)}
        </span>
      )}
    </span>
  );
}
