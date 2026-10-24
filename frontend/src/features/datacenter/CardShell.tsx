import { useEffect } from "react";
import type { ReactNode } from "react";

const LINK_DOT: Record<string, string> = {
  UP: "#34d399",
  DOWN: "#ef4444",
  DISABLED: "#71717a",
  UNKNOWN: "#eab308",
};

export function StateDot({ state }: { state: string }) {
  return <i className="inline-block size-2 rounded-full" style={{ background: LINK_DOT[state] ?? LINK_DOT.UNKNOWN }} />;
}

/** A dialog over the 3D scene: the walker keeps its place and keys typed here never move the camera. */
export function CardShell({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      data-ui-card
      className="absolute inset-0 z-30 flex items-start justify-center overflow-y-auto bg-black/55 p-6 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={label}
        className="relative w-full max-w-5xl rounded-xl border border-[var(--border-strong)] bg-[var(--surface-page)] p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close card"
          className="absolute right-3 top-3 rounded-md border border-[var(--border-subtle)] px-2 py-1 text-sm text-[var(--text-secondary)] hover:bg-[var(--surface-hover)]"
        >
          Close ✕
        </button>
        {children}
      </div>
    </div>
  );
}
