import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import maintenanceIcon from "@/assets/maintenance.svg";
import { ApiError } from "@/api/client";
import { useToggleMaintenanceMutation } from "@/features/inventory/hooks";
import type { ServerRow } from "@/types/server";

/**
 * One row's maintenance switch: entering maintenance asks why, leaving it
 * is one click — the operator's asked-for asymmetry. Inline SVG, not a
 * character: ⏸/🔧 carry emoji presentation and render as a blank box in
 * headless Chromium (`npm run test:e2e`) and on a minimal RHEL desktop.
 */

/** An `<img>` (a fixed multi-colour illustration, nothing for `currentColor`
 * to drive), imported so Vite content-hashes it. */
function ToolsIcon() {
  return <img src={maintenanceIcon} alt="" aria-hidden="true" className="size-4" />;
}

function PlayIcon() {
  return (
    <svg viewBox="0 0 12 12" className="size-3 fill-current" aria-hidden="true">
      <path d="M3 1.5 10.5 6 3 10.5Z" />
    </svg>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem.detail;
  }
  return error instanceof Error ? error.message : "Failed to update maintenance.";
}

export function MaintenanceToggle({ server }: { server: ServerRow }) {
  const toggle = useToggleMaintenanceMutation();
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const enabled = server.maintenance.enabled;

  useEffect(() => {
    if (asking) {
      inputRef.current?.focus();
    }
  }, [asking]);

  function close() {
    setAsking(false);
    setReason("");
    toggle.reset();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    event.stopPropagation();
    const trimmed = reason.trim();
    toggle.mutate(
      { id: server.id, enable: true, ...(trimmed ? { reason: trimmed } : {}) },
      { onSuccess: close },
    );
  }

  const label = enabled
    ? `End maintenance on ${server.name}`
    : `Put ${server.name} into maintenance`;

  return (
    // The whole subtree stops propagation so no click here opens the server.
    <span
      className="relative inline-block"
      onClick={(event) => {
        event.stopPropagation();
      }}
    >
      <button
        type="button"
        title={label}
        aria-label={label}
        aria-expanded={enabled ? undefined : asking}
        disabled={toggle.isPending}
        onClick={() => {
          if (enabled) {
            toggle.mutate({ id: server.id, enable: false });
          } else {
            setAsking(true);
          }
        }}
        className={`inline-flex size-7 items-center justify-center rounded-md border text-xs transition-colors duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] disabled:cursor-not-allowed disabled:opacity-40 ${
          enabled
            ? "border-[var(--border-strong)] bg-[var(--tint-maintenance)] text-[var(--text-on-maintenance)]"
            : "border-[var(--border-subtle)] text-[var(--text-muted)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)]"
        }`}
      >
        {enabled ? <PlayIcon /> : <ToolsIcon />}
      </button>

      {asking && (
        <>
          {/* A plain overlay, not <dialog showModal()>: jsdom 29 does not
              implement showModal, so the native element is untestable. */}
          <span
            className="fixed inset-0 z-20 bg-black/40"
            onClick={close}
            aria-hidden="true"
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label={`Put ${server.name} into maintenance`}
            onKeyDown={(event) => {
              if (event.key === "Escape") close();
            }}
            className="absolute top-full right-0 z-30 mt-1 w-80 rounded-[var(--radius-card)] border border-[var(--border-strong)] bg-[var(--surface-raised)] p-4 text-left shadow-lg"
          >
            <h2 className="text-sm font-semibold text-[var(--text-primary)]">
              Put into maintenance
            </h2>
            <p className="mt-0.5 truncate text-xs text-[var(--text-muted)]" title={server.name}>
              {server.name}
            </p>

            <form onSubmit={submit} className="mt-3">
              <label
                htmlFor={`maint-reason-${server.id}`}
                className="block text-xs text-[var(--text-secondary)]"
              >
                Why is it going into maintenance?
              </label>
              <input
                id={`maint-reason-${server.id}`}
                ref={inputRef}
                type="text"
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                }}
                placeholder="Replacing PSU 2 — INC-4417"
                className="mt-1.5 w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-2 py-1.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]"
              />

              {toggle.isError && (
                <p role="alert" className="mt-2 text-xs text-[var(--text-on-critical)]">
                  {errorMessage(toggle.error)}
                </p>
              )}

              <div className="mt-3 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={close}
                  className="rounded-md border border-[var(--border-subtle)] px-2.5 py-1 text-xs text-[var(--text-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)]"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={toggle.isPending}
                  className="rounded-md border border-[var(--border-strong)] bg-[var(--tint-maintenance)] px-2.5 py-1 text-xs font-medium text-[var(--text-on-maintenance)] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {toggle.isPending ? "Starting…" : "Start maintenance"}
                </button>
              </div>
            </form>
          </div>
        </>
      )}
    </span>
  );
}
