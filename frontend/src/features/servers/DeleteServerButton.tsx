import { useState } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "@/api/client";
import { useAuth } from "@/features/auth/useAuth";
import { useDeleteServerMutation } from "@/features/servers/hooks";
import { showToast } from "@/lib/toast";
import type { ServerDetail } from "@/types/server";

const ADMIN_ONLY_TITLE = "Only admins can remove a server";
const BUTTON =
  "rounded-md px-3 py-1.5 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] disabled:cursor-not-allowed disabled:opacity-40";

/** Why the API refused, when it was `SERVER_DELETE_BLOCKED`; `null` for any other failure. */
function blockersOf(error: unknown): string[] | null {
  if (!(error instanceof ApiError) || error.problem.code !== "SERVER_DELETE_BLOCKED") return null;
  const blockers = error.problem.details.blockers;
  return Array.isArray(blockers) ? blockers.map(String) : [];
}

/** The detail header's "Remove": deletes the server from the database (ADR-0065). Admin only. The
 * name must be typed; a server in use is refused first and only deleted after "Remove anyway". */
export function DeleteServerButton({ server }: { server: ServerDetail }) {
  const { data: me } = useAuth();
  const isAdmin = me?.role !== "VIEWER" && me?.role !== "AUDITOR";
  const navigate = useNavigate();
  const remove = useDeleteServerMutation();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [anyway, setAnyway] = useState(false);
  const blockers = blockersOf(remove.error);
  const otherError = remove.isError && blockers === null;

  function close() {
    setOpen(false);
    setTyped("");
    setAnyway(false);
    remove.reset();
  }

  function submit() {
    remove.mutate(
      { id: server.id, force: anyway },
      {
        onSuccess: () => {
          showToast({ title: `Removed ${server.name}`, detail: "Deleted from the inventory", ok: true });
          void navigate("/servers");
        },
      },
    );
  }

  const confirmed = typed === server.name && (blockers === null || anyway);

  return (
    <>
      <button
        type="button"
        title={isAdmin ? "Delete this server from the inventory" : ADMIN_ONLY_TITLE}
        aria-label={isAdmin ? "Remove this server" : ADMIN_ONLY_TITLE}
        aria-disabled={!isAdmin || undefined}
        onClick={() => {
          if (isAdmin) setOpen(true);
        }}
        className={`inline-flex h-7 items-center rounded-md border border-[var(--border-subtle)] px-2.5 text-xs font-medium text-[var(--text-muted)] transition-colors duration-[var(--duration-instant)] hover:border-[var(--color-status-critical)] hover:text-[var(--text-on-critical)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] ${
          !isAdmin ? "cursor-not-allowed opacity-40 hover:border-[var(--border-subtle)] hover:text-[var(--text-muted)]" : ""
        }`}
      >
        Remove
      </button>
      {open && (
        <>
          <span className="fixed inset-0 z-20 bg-black/40" onClick={close} aria-hidden="true" />
          <div
            role="dialog"
            aria-modal="true"
            aria-label={`Remove ${server.name}`}
            onKeyDown={(event) => {
              if (event.key === "Escape") close();
            }}
            className="fixed top-1/3 left-1/2 z-30 w-[28rem] max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-[var(--radius-card)] border border-[var(--border-strong)] bg-[var(--surface-raised)] p-5 shadow-lg"
          >
            <h2 className="text-base font-semibold text-[var(--text-primary)]">Remove this server?</h2>
            <p className="mt-1 font-mono text-xs break-all text-[var(--text-muted)]">{server.name}</p>
            <p className="mt-3 text-sm text-[var(--text-secondary)]">
              It is deleted from the inventory. If its vendor manager still lists it, the next collector run
              adds it back, without its maintenance or reservation.
            </p>
            {blockers !== null && (
              <div
                role="alert"
                className="mt-3 rounded-md px-3 py-2 text-sm"
                style={{ backgroundColor: "var(--tint-warning)", color: "var(--text-on-warning)" }}
              >
                <p className="font-medium">Not removed, because:</p>
                <ul className="mt-1 list-disc pl-5">
                  {blockers.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
                <label className="mt-2 flex items-center gap-2 font-medium">
                  <input
                    type="checkbox"
                    checked={anyway}
                    onChange={(event) => {
                      setAnyway(event.target.checked);
                    }}
                  />
                  Remove anyway
                </label>
              </div>
            )}
            {otherError && (
              <p role="alert" className="mt-3 text-sm text-[var(--text-on-critical)]">
                {remove.error instanceof Error ? remove.error.message : "Failed to remove the server."}
              </p>
            )}
            <label className="mt-4 block text-xs font-medium text-[var(--text-secondary)]">
              Type the server&apos;s name to confirm
              <input
                type="text"
                value={typed}
                onChange={(event) => {
                  setTyped(event.target.value);
                }}
                className="mt-1 w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-2.5 py-1.5 font-mono text-sm text-[var(--text-primary)]"
              />
            </label>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={close}
                className={`${BUTTON} border border-[var(--border-strong)] text-[var(--text-secondary)]`}
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={!confirmed || remove.isPending}
                onClick={submit}
                className={`${BUTTON} bg-[var(--color-status-critical)] text-white hover:brightness-110`}
              >
                {remove.isPending ? "Removing…" : blockers === null ? "Remove server" : "Remove anyway"}
              </button>
            </div>
          </div>
        </>
      )}
    </>
  );
}
