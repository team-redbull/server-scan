import { useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";

import { copyText } from "@/lib/clipboard";

const RESET_AFTER_MS = 1600;

type State = "idle" | "copied" | "failed";

function CopyIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      className="size-3.5 fill-none stroke-current"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.75" />
      <path
        d="M10.5 3.5v-.25A1.75 1.75 0 0 0 8.75 1.5h-5A1.75 1.75 0 0 0 2 3.25v5A1.75 1.75 0 0 0 3.75 10H4"
        strokeLinecap="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      className="size-3.5 fill-none stroke-current"
      strokeWidth="1.75"
      aria-hidden="true"
    >
      <path d="m3.5 8.5 3 3 6-7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Icon-only button that copies `text` and confirms in place: it presses in
 * on pointer-down, the icon crossfades to a green check, and a live region
 * announces the result. The swap is critically damped (no overshoot: a tap
 * carries no momentum) and degrades to a plain fade under reduced motion.
 * Sized like `BmcLink`'s icon so the two sit together in the detail header.
 * `revealOnRowHover` (inventory rows, which carry `group`) keeps it hidden
 * until the row is hovered or the button focused; touch screens always show it.
 * `compact` is 24px instead of 28px, for the width-starved inventory table. */
export function CopyButton({
  text,
  label,
  revealOnRowHover = false,
  compact = false,
}: {
  text: string;
  label: string;
  revealOnRowHover?: boolean;
  compact?: boolean;
}) {
  const [state, setState] = useState<State>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(
    () => () => {
      clearTimeout(timer.current);
    },
    [],
  );

  const onClick = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    void copyText(text).then((ok) => {
      setState(ok ? "copied" : "failed");
      clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        setState("idle");
      }, RESET_AFTER_MS);
    });
  };

  const copied = state === "copied";
  const tint = copied
    ? "border-[var(--color-status-healthy)] text-[var(--text-on-healthy)]"
    : state === "failed"
      ? "border-[var(--color-status-critical)] text-[var(--text-on-critical)]"
      : "border-[var(--border-subtle)] text-[var(--text-muted)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)]";

  const reveal =
    revealOnRowHover && state === "idle"
      ? "opacity-0 group-hover:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100"
      : "";

  return (
    <>
      <button
        type="button"
        onClick={onClick}
        title={label}
        aria-label={label}
        className={`relative inline-flex ${compact ? "size-6" : "size-7"} items-center justify-center rounded-md border transition-[color,border-color,transform,opacity] duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] active:scale-90 motion-reduce:active:scale-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] ${tint} ${reveal}`}
      >
        <span
          className={`absolute transition-[opacity,transform] duration-[var(--duration-fast)] ease-[var(--ease-out-strong)] ${copied ? "scale-50 opacity-0 motion-reduce:scale-100" : "scale-100 opacity-100"}`}
        >
          <CopyIcon />
        </span>
        <span
          className={`absolute transition-[opacity,transform] duration-[var(--duration-fast)] ease-[var(--ease-out-strong)] ${copied ? "scale-100 opacity-100" : "scale-50 opacity-0 motion-reduce:scale-100"}`}
        >
          <CheckIcon />
        </span>
      </button>
      <span role="status" aria-live="polite" className="sr-only">
        {copied ? "Copied" : state === "failed" ? "Copy failed" : ""}
      </span>
    </>
  );
}
